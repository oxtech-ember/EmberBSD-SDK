#!/usr/bin/env ruby
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted dispatch and filesystem contract tests.
# These fixtures do not build or qualify an EmberBSD sysroot.

require 'tmpdir'
require_relative '../lib/sysroot_builder'

module BuilderTests
  module_function

  def assert(value, message = 'assertion failed')
    raise message unless value
  end

  def reject(message = nil)
    yield
    raise 'expected builder rejection'
  rescue PrebuildSDK::Error, SystemCallError => e
    assert(e.message.include?(message), "expected #{message.inspect}, got #{e.message.inspect}") if message
  end

  def write(path, data, mode = 0o644)
    FileUtils.mkdir_p(File.dirname(path))
    File.binwrite(path, data)
    File.chmod(mode, path)
  end

  def elf(machine, type)
    data = "\0".b * 64
    data[0, 7] = "\x7fELF\x02\x01\x01".b
    data[16, 8] = [type, machine, 1].pack('vvV')
    data[52, 2] = [64].pack('v')
    data
  end

  def archive(data, name = 'fixture.o/')
    header = name.ljust(16) + '0'.ljust(12) + '0'.ljust(6) + '0'.ljust(6) + '100644'.ljust(8) + data.bytesize.to_s.ljust(10) + "`\n"
    "!<arch>\n".b + header + data + (data.bytesize.odd? ? "\n" : '')
  end

  def git(path, *args)

    stdout, stderr, status = Open3.capture3('git', '-C', path, *args)
    assert(status.success?, stderr)
    stdout.strip
  end

  def commit(path)
    git(path, 'add', '-f', '.')
    git(path, '-c', 'user.name=SDK fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture source')
  end

  def snapshot(root)
    result = {}
    Find.find(root) do |path|
      next if path.include?('/.git/') || path.end_with?('/.git')
      s = File.lstat(path)
      result[path] = [s.ino, s.mode, s.size, s.mtime.to_r,
                      s.file? ? Digest::SHA256.file(path).hexdigest : s.symlink? ? File.readlink(path) : nil]
    end
    result
  end

  def manifest(work)
    root = File.join(work, 'stage/usr/pkg/gcc16')
    rows = PrebuildSDK::Inventory.tree(root)
    files = rows.select { |_, r| r['kind'] == 'file' }.map { |p, r| "#{r['sha256']}  ./usr/pkg/gcc16/#{p}\n" }.join
    links = rows.select { |_, r| r['kind'] == 'link' }.map { |p, r| "./usr/pkg/gcc16/#{p}\t#{r['target']}\n" }.join
    write(File.join(work, 'stage.sha256'), files)
    write(File.join(work, 'stage.symlinks'), links)
    report_path = File.join(work, 'report.json')
    report = JSON.parse(File.read(report_path))
    %w[inputs.txt stage.sha256 stage.symlinks].zip(%w[GCC_RUNTIME_INPUTS_SHA256 GCC_RUNTIME_STAGE_SHA256 GCC_RUNTIME_LINKS_SHA256]).each do |file, key|
      report['metadata'][key] = Digest::SHA256.file(File.join(work, file)).hexdigest
    end
    write(report_path, JSON.generate(report))
  end

  def fixture(root, target = 'aarch64')
    t = PrebuildSDK::SysrootBuilder::TARGETS.fetch(target)
    %w[os ports compiler runtime pkgsrc].each { |name| Dir.mkdir(File.join(root, name)) }
    sdk = File.join(root, 'sdk')
    PrebuildSDK.create(sdk)
    os = File.join(root, 'os')
    ports = File.join(root, 'ports')
    compiler = File.join(root, 'compiler')
    work = File.join(root, 'runtime')
    control = File.join(root, 'control.json')
    write(control, '{}')
    %w[include lib libexec/ld.elf_so tools share/mk sys distrib etc].each { |p| FileUtils.mkdir_p(File.join(os, p)) }
    %w[Makefile libexec/ld.elf_so/Makefile share/mk/bsd.own.mk].each { |p| write(File.join(os, p), "# Fixture\n") }
    tool = <<~CODE
      #!#{RbConfig.ruby}
      require 'json'
      c = JSON.parse(File.read(#{control.inspect}))
      case ARGV.fetch(0)
      when '-dumpfullversion' then puts c.fetch('version', '16.2.0')
      when '-dumpmachine' then puts c.fetch('triplet', #{t.fetch('triplet').inspect})
      when '-v' then warn 'Thread model: ' + c.fetch('threads', 'posix')
      when /\\A-print-prog-name=/
        puts File.join(#{compiler.inspect}, 'bin', ARGV[0].split('=', 2)[1])
      else abort 'Unexpected fixture compiler invocation'
      end
    CODE
    %w[gcc g++ cpp ar as ld nm objcopy objdump ranlib readelf strip].each do |name|
      write(File.join(compiler, 'bin', t.fetch('triplet') + '-' + name), tool, 0o755)
    end
    %w[cc1 cc1plus collect2 lto1].each { |name| write(File.join(compiler, 'bin', name), "#!/bin/sh\nexit 0\n", 0o755) }
    %w[as ld].each { |name| File.symlink(t.fetch('triplet') + '-' + name, File.join(compiler, 'bin', name)) }
    prefix = File.join(work, 'stage/usr/pkg/gcc16')
    %W[include/c++/vector include/c++/#{t.fetch('triplet')}/bits/c++config.h include/c++/backward/backward_warning.h].each { |p| write(File.join(prefix, p), "// Fixture\n") }
    %w[crtbegin.o crtbeginS.o crtend.o crtendS.o].each { |p| write(File.join(prefix, 'lib/gcc', t.fetch('triplet'), '16.2.0', p), elf(t.fetch('elf'), 1)) }
    %W[lib/gcc/#{t.fetch('triplet')}/16.2.0/libgcc.a lib/gcc/#{t.fetch('triplet')}/16.2.0/libgcc_eh.a lib/libstdc++.a lib/libsupc++.a].each { |p| write(File.join(prefix, p), archive(elf(t.fetch('elf'), 1))) }
    %w[libgcc_s.so.1 libstdc++.so.7].each { |p| write(File.join(prefix, 'lib', p), elf(t.fetch('elf'), 3)) }
    File.symlink('libstdc++.so.7', File.join(prefix, 'lib/libstdc++.so'))
    write(File.join(prefix, 'empty'), '')
    write(File.join(work, 'inputs.txt'), "Fixture provenance\n")
    write(File.join(work, 'report.json'), JSON.generate('metadata' => {
      'OPSYS' => 'NetBSD', 'OS_VERSION' => '11.0', 'GCC_VERSION' => '16.2.0',
      'MACHINE_ARCH' => t.fetch('arch'), 'GCC_RUNTIME_MACHINE_ARCH' => t.fetch('arch'),
      'GCC_RUNTIME_TARGET' => target, 'GCC_RUNTIME_TRIPLET' => t.fetch('triplet'),
      'EMBERBSD_SEED_SOURCE' => 'a' * 40, 'fixture_only' => true
    }))
    manifest(work)
    verifier = <<~CODE
      require 'json'
      abort 'Expected normal check dispatch' unless ARGV.length == 4 && ARGV[0] == 'check'
      c = JSON.parse(File.read(#{control.inspect}))
      abort 'Injected verifier failure' if c['runtime_fail']
      puts File.read(File.join(ARGV[1], 'report.json'))
    CODE
    write(File.join(ports, PrebuildSDK::SysrootBuilder::VERIFIER), verifier)
    producer = <<~CODE
      #!#{RbConfig.ruby}
      require 'json'
      require 'fileutils'
      c = JSON.parse(File.read(#{control.inspect}))
      args = ARGV.dup
      def value(args, flag); args.fetch(args.index(flag) + 1); end
      abort 'Wrong producer operations' unless args.last(2) == %w[tools libs]
      abort 'Missing update mode' unless args.include?('-u') && args.include?('-U')
      abort 'Incorrect target' unless value(args, '-m') == #{t.fetch('machine').inspect} && value(args, '-a') == #{t.fetch('arch').inspect}
      abort 'Inherited target compiler' if ENV['CC'] || ENV['CXX'] || ENV['CFLAGS'] || ENV['MAKEFLAGS']
      dest = value(args, '-D')
      File.write(File.join(dest, 'producer-argv.json'), JSON.generate(args))
      abort 'Injected build failure' if c['build_fail']
      if c['build_wait']
        if c['build_ignore_child']
          fork do
            Signal.trap('TERM', 'IGNORE')
            File.write(File.join(dest, 'descendant-pid'), Process.pid.to_s)
            loop do
              File.open(File.join(value(args, '-O'), 'fixture-writer.log'), 'a') { |file| file.write('writing\\n') }
              sleep 0.05
            end
          end
        end
        File.write(File.join(dest, 'child-pid'), Process.pid.to_s)
        if c.key?('build_orphan_exit')
          sleep 0.01 until File.file?(File.join(dest, 'descendant-pid'))
          exit Integer(c['build_orphan_exit'])
        end
        sleep 60

      end
      %w[usr/include/stdio.h usr/include/sys/types.h usr/include/math.h usr/include/pthread.h].each do |p|
        FileUtils.mkdir_p(File.dirname(File.join(dest, p)))
        File.write(File.join(dest, p), '// Fixture')
      end
      FileUtils.mkdir_p(File.join(dest, 'usr/lib'))
      %w[crt0.o crti.o crtn.o].each { |p| File.binwrite(File.join(dest, 'usr/lib', p), #{elf(t.fetch('elf'), 1).inspect}) }
      %w[libc.so libm.so libpthread.so].each { |p| File.binwrite(File.join(dest, 'usr/lib', p), #{elf(t.fetch('elf'), 3).inspect}) }
      File.unlink(File.join(dest, 'usr/include/stdio.h')) if c['missing_header']
      File.binwrite(File.join(dest, 'usr/lib/crt0.o'), #{elf(999, 1).inspect}) if c['wrong_elf']
      File.symlink('/outside', File.join(dest, 'absolute')) if c['absolute_link']
      File.symlink('../../control.json', File.join(dest, 'escape')) if c['escaped_link']
      File.symlink('missing', File.join(dest, 'dangling')) if c['dangling_link']
      File.write(File.join(dest, 'empty-file'), '')
      wrapper = File.join(value(args, '-T'), 'bin', 'nbmake-#{t.fetch('machine')}')
      FileUtils.mkdir_p(File.dirname(wrapper))
      File.write(wrapper, "#!#{RbConfig.ruby}\\n" + <<~'WRAPPER')
        require 'fileutils'
        require 'json'
        c = JSON.parse(File.read(#{control.inspect}))
        dest = #{sdk.inspect} + '/sysroot'
        # Read the selected destination from the sibling fixture configuration.
        dest = File.read(File.join(File.dirname(__FILE__), 'fixture-destination'))
        case ARGV.last
        when 'dependall'
          abort 'Injected loader failure' if c['loader_fail']
          File.write(File.join(dest, 'loader-dependall'), 'done')
        when 'install'
          abort 'Install before dependall' unless File.file?(File.join(dest, 'loader-dependall'))
          FileUtils.mkdir_p(File.join(dest, 'libexec'))
          File.binwrite(File.join(dest, 'libexec/ld.elf_so'), #{elf(t.fetch('elf'), 3).inspect})
        else abort 'Wrong loader dispatch'
        end
      WRAPPER
      File.write(File.join(File.dirname(wrapper), 'fixture-destination'), dest)
      File.chmod(0755, wrapper)
    CODE
    write(File.join(os, 'fixture-producer.rb'), producer)
    write(File.join(os, 'build.sh'), "#!/bin/sh\nexec #{RbConfig.ruby} #{File.join(os, 'fixture-producer.rb')} \"$@\"\n", 0o755)
    [os, ports].each { |path| git(path, 'init', '-q'); commit(path) }
    archive = File.join(root, 'gcc.tar.xz')
    write(archive, 'Fixture archive; no extraction')
    { 'sdk' => sdk, 'os' => os, 'ports' => ports, 'compiler' => compiler, 'runtime-work' => work,
      'gcc-archive' => archive, 'pkgsrc' => File.join(root, 'pkgsrc'), 'target' => target, 'jobs' => '2' }
  end

  def control(options, changes)
    write(File.join(File.dirname(options.fetch('sdk')), 'control.json'), JSON.generate(changes))
  end

  def test(name)
    return if ENV['PREBUILD_SDK_TEST_CASE'] && ENV['PREBUILD_SDK_TEST_CASE'] != name
    Dir.mktmpdir('sdk-sysroot-') do |root|
      root = File.realpath(root)
      options = fixture(root)
      yield options, root
    end
    puts "PASS #{name}"
    @count = (@count || 0) + 1
  end

  def builder(options)
    PrebuildSDK::SysrootBuilder.new(options)
  end

  def run
    test('read-only plan and exact native commands') do |o, root|
      before = snapshot(root)
      r = builder(o.merge('plan' => true)).run
      assert(snapshot(root) == before, 'plan mutated fixture')
      assert(r['qualification'] == 'not-built' && r['execution_acceptance'] == 'not-run')
      assert(r['plan'][0][2] == 'check')
      assert(r['plan'][1].last(2) == %w[tools libs])
      assert(r['plan'][1].include?('MKCXX=yes') && r['plan'][1].include?('MKGCCCMDS=no'))
      assert(!r['plan'][1].include?('MKOBJDIRS=no'))
      %w[MKLLVM=no MKLLVMRT=no MKLIBCXX=no].each { |flag| assert(r['plan'][1].include?(flag)) }
      assert(r['plan'][2].last == 'dependall' && r['plan'][3].last == 'install')
      assert(r['paths']['destination'] == o['sdk'] + '/sysroot')
    end
    test('actual subprocess dispatch and output inspection') do |o, _|
      old = ENV['CC']; ENV['CC'] = 'clang'
      begin
        r = builder(o).run
      ensure
        old ? ENV['CC'] = old : ENV.delete('CC')
      end
      assert(r['qualification'] == 'built-and-inspected')
      actual = r['commands'].reject { |c| c['scope'] == 'runtime-check' }
      assert(actual.length == 3 && actual.all? { |c| c['exit_status'] == 0 })
      assert(r['commands'].count { |c| c['scope'] == 'runtime-check' } == 3)
      assert(!File.exist?(o['sdk'] + '/sysroot/' + PrebuildSDK::SysrootBuilder::MARKER))
      assert(r['outputs']['usr/pkg/gcc16/lib/libstdc++.so']['kind'] == 'link')
      assert(r['outputs']['empty-file']['size'] == 0)
      assert(!File.exist?(o['sdk'] + '/sysroot/var/db/pkg'))
    end
    test('audited directory modes survive a restrictive umask') do |o, _|
      prefix = o['runtime-work'] + '/stage/usr/pkg/gcc16'
      File.chmod(0o775, prefix + '/include')
      File.chmod(0o555, prefix + '/include/c++/backward')
      previous = File.umask(0o077)
      begin
        r = builder(o).run
        assert(r['outputs']['usr/pkg/gcc16/include']['mode'] == 0o775)
        assert(r['outputs']['usr/pkg/gcc16/include/c++/backward']['mode'] == 0o555)
        assert(r['outputs']['usr/pkg/gcc16']['mode'] == (File.stat(prefix).mode & 0o7777))
      ensure
        File.umask(previous)
        File.chmod(0o755, prefix + '/include/c++/backward')
        copied = o['sdk'] + '/sysroot/usr/pkg/gcc16/include/c++/backward'
        File.chmod(0o755, copied) if File.directory?(copied)
      end
    end
    test('explicit stage through the workspace alias uses canonical paths') do |o, root|
      alias_path = root + '/prebuild-sdk'
      File.symlink(o['sdk'], alias_path)
      r = builder(o.merge('sdk' => alias_path, 'destination' => alias_path + '/stages/next', 'plan' => true)).run
      assert(r['paths']['destination'] == o['sdk'] + '/stages/next')
    end
    test('amd64 target dispatch') do |_, root|
      other = File.join(root, 'amd64'); Dir.mkdir(other)
      r = builder(fixture(other, 'amd64')).run
      assert(r['outputs']['usr/lib/crt0.o']['elf']['machine'] == 62)
    end
    test('nonempty active destination is unchanged') do |o, root|
      write(o['sdk'] + '/sysroot/active', 'preserve')
      before = snapshot(root)
      reject('destination must be new or empty') { builder(o).run }
      assert(snapshot(root) == before)
    end
    %w[version triplet threads].zip(%w[15.2.0 other none]).each do |key, bad|
      test("reject compiler #{key}") do |o, root|
        control(o, key => bad)
        before = snapshot(root)
        reject('expected GCC') { builder(o).run }
        assert(snapshot(root) == before)
      end
    end
    test('normal runtime check is mandatory') do |o, root|
      control(o, 'runtime_fail' => true)
      before = snapshot(root)
      reject('Injected verifier failure') { builder(o).run }
      assert(snapshot(root) == before)
    end
    test('runtime target metadata mismatch') do |o, _|
      path = o['runtime-work'] + '/report.json'
      report = JSON.parse(File.read(path)); report['metadata']['OPSYS'] = 'Linux'
      write(path, JSON.generate(report))
      reject('runtime target metadata mismatch') { builder(o).run }
    end
    test('actual runtime bytes must match audited manifest') do |o, _|
      write(o['runtime-work'] + '/stage/usr/pkg/gcc16/include/c++/vector', 'changed')
      reject('does not match') { builder(o).run }
    end
    test('runtime receipt hashes are enforced') do |o, _|
      File.open(o['runtime-work'] + '/stage.sha256', 'a') { |f| f.write('extra') }
      reject('runtime receipt changed') { builder(o).run }
    end
    test('missing developer CRT') do |o, _|
      File.unlink(o['runtime-work'] + '/stage/usr/pkg/gcc16/lib/gcc/aarch64--netbsd/16.2.0/crtbegin.o')
      manifest(o['runtime-work'])
      reject { builder(o).run }
    end
    test('dirty OS source rejected') do |o, _|
      write(o['os'] + '/Makefile', 'dirty')
      reject('clean Git root') { builder(o).run }
    end
    test('dirty Ports source rejected') do |o, _|
      write(o['ports'] + '/untracked', 'dirty')
      reject('clean Git root') { builder(o).run }
    end
    test('missing source subtree rejected') do |o, _|
      Dir.rmdir(o['os'] + '/lib')
      reject('real directory') { builder(o).run }
    end
    %w[build_fail loader_fail missing_header wrong_elf absolute_link escaped_link dangling_link].each do |failure|
      test("retain incomplete destination on #{failure}") do |o, _|
        control(o, failure => true)
        reject { builder(o).run }
        path = o['sdk'] + '/sysroot/'
        r = JSON.parse(File.read(path + PrebuildSDK::SysrootBuilder::RECEIPT))
        assert(r['qualification'] == 'incomplete' && r['error'])
        assert(File.file?(path + PrebuildSDK::SysrootBuilder::MARKER))
        assert(File.file?(path + 'producer-argv.json'))
        reject('new or empty') { builder(o).run }
      end
    end
    test('unknown object cache is not modified') do |o, root|
      plan = builder(o.merge('plan' => true)).run
      objects = plan['paths']['objects']
      write(objects + '/foreign', 'preserve')
      before = snapshot(root)
      reject { builder(o).run }
      assert(snapshot(root) == before)
    end
    test('reuse compatible object context and source fast-forward') do |o, _|
      first = builder(o).run
      write(o['os'] + '/revision', 'next'); commit(o['os'])
      stage = o.merge('destination' => o['sdk'] + '/stages/next')
      next_plan = builder(stage.merge('plan' => true)).run
      assert(next_plan['previous_contexts']['objects']['sources']['os']['commit'] == first['sources']['os']['commit'])
      assert(next_plan['sources']['os']['commit'] != first['sources']['os']['commit'])
      result = builder(stage).run
      assert(result['qualification'] == 'built-and-inspected')
      assert(result['paths']['objects'] == first['paths']['objects'])
    end
    test('incompatible compiler cache cannot be reused') do |o, root|
      builder(o).run
      File.open(o['compiler'] + '/bin/aarch64--netbsd-gcc', 'a') { |f| f.write("# changed\n") }
      before = snapshot(root)
      reject('incompatible object context') { builder(o.merge('destination' => o['sdk'] + '/next')).run }
      assert(snapshot(root) == before)
    end
    test('non-ancestor OS source cannot reuse cache') do |o, _|
      builder(o).run
      git(o['os'], 'checkout', '--orphan', 'other')
      commit(o['os'])
      reject('command failed') { builder(o.merge('destination' => o['sdk'] + '/next')).run }
    end
    test('symlink destination and object aliases rejected') do |o, root|
      File.symlink(o['sdk'] + '/sysroot', o['sdk'] + '/alias')
      reject('not a real directory') { builder(o.merge('destination' => o['sdk'] + '/alias')).run }
      File.symlink(o['sdk'] + '/packages', o['sdk'] + '/obj/alias')
      reject('not a real directory') { builder(o.merge('objects' => o['sdk'] + '/obj/alias')).run }
      reject('output must be inside') { builder(o.merge('destination' => root + '/outside')).run }
      reject('overlapping') { builder(o.merge('destination' => o['sdk'] + '/obj/new')).run }
    end
    test('same object and tools paths rejected') do |o, _|
      reject('overlapping') { builder(o.merge('objects' => o['sdk'] + '/obj/shared', 'tools' => o['sdk'] + '/obj/shared')).run }
    end
    test('copy ABA rejected against pre-copy accepted inventory') do |o, _|
      original = IO.method(:copy_stream)
      singleton = IO.singleton_class
      target = o['runtime-work'] + '/stage/usr/pkg/gcc16/include/c++/vector'
      touched = false
      singleton.send(:remove_method, :copy_stream)
      singleton.send(:define_method, :copy_stream) do |input, output|
        if input.path == target
          old = File.binread(target)
          File.binwrite(target, '// corrupt during copy')
          begin
            original.call(input, output)
          ensure
            File.binwrite(target, old)
          end
          touched = true
        else
          original.call(input, output)
        end
      end
      begin
        reject('copied runtime differs') { builder(o).run }
        assert(touched)
      ensure
        singleton.send(:remove_method, :copy_stream)
        singleton.send(:define_method, :copy_stream, original)
      end
    end
    test('required ELF type and truncated ELF are rejected') do |o, _|
      path = o['runtime-work'] + '/stage/usr/pkg/gcc16/lib/gcc/aarch64--netbsd/16.2.0/crtbegin.o'
      write(path, elf(183, 3)); manifest(o['runtime-work'])
      reject('wrong ELF type') { builder(o).run }
      write(path, "\x7fELF"); manifest(o['runtime-work'])
      reject('ELF64 little-endian') { builder(o).run }
    end
    test('archive member machine, format and truncation checks') do |o, _|
      path = o['runtime-work'] + '/stage/usr/pkg/gcc16/lib/libstdc++.a'
      write(path, archive(elf(62, 1))); manifest(o['runtime-work'])
      reject('ELF64 little-endian') { builder(o).run }
      write(path, archive('text')); manifest(o['runtime-work'])
      reject('non-ELF archive member') { builder(o).run }
      write(path, archive(elf(183, 1)).chop); manifest(o['runtime-work'])
      reject('truncated archive member') { builder(o).run }
      write(path, "!<thin>\n"); manifest(o['runtime-work'])
      reject('normal static archive') { builder(o).run }
    end
    test('TERM stops own child before releasing context locks') do |o, root|
      control(o, 'build_wait' => true)
      cli = File.expand_path('../scripts/build-sysroot.rb', __dir__)
      log = File.join(root, 'signal.log')
      pid = Process.spawn(RbConfig.ruby, cli, *o.flat_map { |k, v| ['--' + k, v] }, out: log, err: log)
      begin
        child_file = o['sdk'] + '/sysroot/child-pid'
        Timeout.timeout(60) { sleep 0.05 until File.file?(child_file) }
        child = Integer(File.read(child_file))
        Process.kill('TERM', pid)
        _, status = Timeout.timeout(20) { Process.wait2(pid) }
        pid = nil
        assert(status.exitstatus == 130, File.read(log))
        begin
          Process.kill(0, child)
          raise 'child build survived interruption'
        rescue Errno::ESRCH
          # Expected: the child process has stopped.
        end
        r = JSON.parse(File.read(o['sdk'] + '/sysroot/' + PrebuildSDK::SysrootBuilder::RECEIPT))
        assert(r['qualification'] == 'incomplete' && r['error'].include?('TERM'))
        assert(File.file?(o['sdk'] + '/sysroot/' + PrebuildSDK::SysrootBuilder::MARKER))
      ensure
        if pid
          Process.kill('TERM', pid) rescue Errno::ESRCH
          Process.wait(pid) rescue Errno::ECHILD
        end
      end
    end
    test('TERM drains a TERM-ignoring descendant process group') do |o, root|
      control(o, 'build_wait' => true, 'build_ignore_child' => true)
      cli = File.expand_path('../scripts/build-sysroot.rb', __dir__)
      log = File.join(root, 'descendant-signal.log')
      pid = Process.spawn(RbConfig.ruby, cli, *o.flat_map { |k, v| ['--' + k, v] }, out: log, err: log)
      descendant = nil
      begin
        descendant_file = o['sdk'] + '/sysroot/descendant-pid'
        Timeout.timeout(60) { sleep 0.05 until File.file?(descendant_file) && !File.empty?(descendant_file) }
        descendant = Integer(File.read(descendant_file))
        Process.kill('TERM', pid)
        _, status = Timeout.timeout(20) { Process.wait2(pid) }
        pid = nil
        assert(status.exitstatus == 130, File.read(log))
        begin
          Process.kill(0, descendant)
          raise 'descendant writer survived interruption'
        rescue Errno::ESRCH
          descendant = nil
        end
        r = JSON.parse(File.read(o['sdk'] + '/sysroot/' + PrebuildSDK::SysrootBuilder::RECEIPT))
        writer = r['paths']['objects'] + '/fixture-writer.log'
        digest = Digest::SHA256.file(writer).hexdigest
        sleep 0.2
        assert(Digest::SHA256.file(writer).hexdigest == digest, 'writes continued after CLI returned')
        assert(r['qualification'] == 'incomplete' && r['error'].include?('TERM'))
        assert(File.file?(o['sdk'] + '/sysroot/' + PrebuildSDK::SysrootBuilder::MARKER))
      ensure
        if pid
          Process.kill('TERM', pid) rescue Errno::ESRCH
          Process.wait(pid) rescue Errno::ECHILD
        end
        Process.kill('KILL', descendant) if descendant
      end
    end
    test('GNU and BSD archive name and index records') do |_, root|
      path = File.join(root, 'names.a')
      body = elf(183, 1)
      write(path, archive('index', '/') + archive('long-name.o/\n', '//').byteslice(8..) + archive(body, '/0').byteslice(8..))
      assert(PrebuildSDK::Inventory.archive(path, 183)['elf_members'] == 1)
      name = 'a-long-object-name.o' + "\0"
      index = "__.SYMDEF SORTED\0"
      write(path, archive(index + 'index', '#1/' + index.bytesize.to_s) + archive(name + body, '#1/' + name.bytesize.to_s).byteslice(8..))
      assert(PrebuildSDK::Inventory.archive(path, 183)['elf_members'] == 1)
    end
    test('busy and quarantined contexts reject reuse') do |o, root|
      plan = builder(o.merge('plan' => true)).run
      context = %w[objects tools].map { |name| plan['paths'][name] }.sort.first
      lock = o['sdk'] + '/obj/.prebuild-sdk-lock-' + Digest::SHA256.hexdigest(context)
      File.open(lock, 'w') do |file|
        file.flock(File::LOCK_EX)
        before = snapshot(root)
        reject('context is busy') { builder(o).run }
        assert(snapshot(root) == before)
      end
      r = builder(o).run
      write(r['paths']['objects'] + '/' + PrebuildSDK::SysrootBuilder::QUARANTINE, '{}')
      before = snapshot(root)
      reject('quarantined') { builder(o.merge('destination' => o['sdk'] + '/next', 'plan' => true)).run }
      assert(snapshot(root) == before)
    end
    test('exited producer cannot leave a writer behind') do |o, _|
      [7, 0].each do |exit_status|
        control(o, 'build_wait' => true, 'build_ignore_child' => true, 'build_orphan_exit' => exit_status)
        destination = o['sdk'] + '/stage-' + exit_status.to_s
        descendant = nil
        begin
          error = nil
          begin
            builder(o.merge('destination' => destination)).run
          rescue PrebuildSDK::Error => e
            error = e
          end
          descendant = Integer(File.read(destination + '/descendant-pid'))
          begin
            Process.kill(0, descendant)
            raise 'exited producer left its writer alive'
          rescue Errno::ESRCH
            descendant = nil
          end
          assert(error && error.message.include?('producer left live descendants'))
          r = JSON.parse(File.read(destination + '/' + PrebuildSDK::SysrootBuilder::RECEIPT))
          assert(r['qualification'] == 'incomplete')
          writer = r['paths']['objects'] + '/fixture-writer.log'
          digest = Digest::SHA256.file(writer).hexdigest
          sleep 0.2
          assert(Digest::SHA256.file(writer).hexdigest == digest, 'writes continued after failure returned')
        ensure
          Process.kill('KILL', descendant) if descendant
        end
      end
    end
    test('shared Binutils aliases are pinned and invalidate incompatible cache') do |o, root|
      shared = root + '/shared-binutils'; Dir.mkdir(shared)
      %w[ar as ld nm objcopy objdump ranlib readelf strip].each do |name|
        alias_path = o['compiler'] + '/bin/aarch64--netbsd-' + name
        provider = shared + '/' + name
        FileUtils.mv(alias_path, provider)
        File.symlink(provider, alias_path)
      end
      r = builder(o).run
      binaries = r['signature']['compiler']['binaries']
      assert(binaries['as']['path'] == shared + '/as')
      assert(binaries['resolved-as']['path'] == binaries['as']['path'])
      assert(binaries['as']['selected_path'] == o['compiler'] + '/bin/aarch64--netbsd-as')
      File.open(shared + '/as', 'a') { |file| file.write("# changed provider\n") }
      before = snapshot(root)
      reject('incompatible object context') { builder(o.merge('destination' => o['sdk'] + '/next')).run }
      assert(snapshot(root) == before)
    end
    test('external frontends and split driver Binutils providers are rejected') do |o, root|
      frontend = o['compiler'] + '/bin/aarch64--netbsd-gcc'
      other = root + '/gcc'
      FileUtils.mv(frontend, other); File.symlink(other, frontend)
      reject('frontend escapes prefix') { builder(o).run }
      File.unlink(frontend); FileUtils.mv(other, frontend)
      File.unlink(o['compiler'] + '/bin/as')
      write(o['compiler'] + '/bin/as', "#!/bin/sh\nexit 0\n", 0o755)
      reject('different Binutils providers') { builder(o).run }
    end
    test('CLI plan produces JSON and no output mutations') do |o, root|
      before = snapshot(root)
      command = [RbConfig.ruby, File.expand_path('../scripts/build-sysroot.rb', __dir__), *o.flat_map { |k, v| ['--' + k, v] }, '--plan']
      stdout, stderr, status = Open3.capture3(*command)
      assert(status.success?, stderr)
      assert(JSON.parse(stdout)['qualification'] == 'not-built')
      assert(snapshot(root) == before)
    end
    raise 'no matching test case' unless @count && @count.positive?
    puts "#{@count} dispatch/filesystem contracts passed; no real target build or execution"
  end
end

BuilderTests.run if $PROGRAM_NAME == __FILE__
