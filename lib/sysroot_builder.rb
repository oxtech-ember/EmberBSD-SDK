# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted orchestration of OS and Ports contracts.

require 'fileutils'
require 'open3'
require 'rbconfig'
require 'timeout'
require_relative 'sysroot_inventory'

module PrebuildSDK
  class SysrootBuilder
    TARGETS = {
      'aarch64' => { 'machine' => 'evbarm', 'arch' => 'aarch64', 'triplet' => 'aarch64--netbsd', 'elf' => 183 },
      'amd64' => { 'machine' => 'amd64', 'arch' => 'x86_64', 'triplet' => 'x86_64--netbsd', 'elf' => 62 }
    }.freeze
    FLAGS = %w[TOOLCHAIN_MISSING=yes MKGCCCMDS=no MKCOMPAT=no MKCXX=yes
               MKDEBUG=no MKDEBUGLIB=no MKX11=no MKLLVM=no MKLLVMRT=no MKLIBCXX=no MKKMOD=no MKGDB=no MKBINUTILS=no
               MKATF=no MKKYUA=no MKMAN=no MKDOC=no MKINFO=no MKHTML=no MKLINT=no
               MKPROFILE=no MKCVS=no MKGROFF=no MKRUMP=no MAKECONF=/dev/null].freeze
    VERIFIER = 'profiles/development-toolchain/runtime/recipe/files/runtime.rb'.freeze
    CONTEXT = '.prebuild-sdk-context.json'.freeze
    QUARANTINE = '.prebuild-sdk-quarantine.json'.freeze
    RECEIPT = '.prebuild-sdk-build.json'.freeze
    MARKER = '.prebuild-sdk-build-incomplete'.freeze
    LOG = '.prebuild-sdk-build.log'.freeze

    def initialize(options)
      @options = options
      @commands = []
      @locks = []
      # Use an explicit environment. Ambient target CC, CXX, flags, make
      # options and compiler search paths must not override the selected GCC.
      @environment = ENV.to_h.select { |key, _| %w[PATH HOME TMPDIR TMP TEMP USER LOGNAME].include?(key) }
      @environment.merge!('LC_ALL' => 'C', 'MAKECONF' => '/dev/null',
                          'GIT_OPTIONAL_LOCKS' => '0', 'GIT_NO_LAZY_FETCH' => '1')
    end

    def capture(*argv, directory: nil)
      options = { unsetenv_others: true }
      options[:chdir] = directory if directory
      stdout, stderr, status = Open3.capture3(@environment, *argv, **options)
      raise Error, "command failed (#{status.exitstatus}): #{argv.inspect}: #{stderr.strip}" unless status.success?

      [stdout, stderr]
    end

    def canonical(value)
      PrebuildSDK.path(File.realpath(PrebuildSDK.path(value)))
    end

    def executable(value)
      path = canonical(value)
      Inventory.regular(path)
      raise Error, "not executable: #{path}" unless File.executable?(path)

      path
    end

    def find_command(name)
      candidate = @environment.fetch('PATH', '').split(File::PATH_SEPARATOR).map do |dir|
        File.join(dir, name) unless dir.empty?
      end.compact.find { |path| File.file?(path) && File.executable?(path) }
      raise Error, "required host command is unavailable: #{name}" unless candidate

      executable(candidate)
    end

    def git(root, *arguments)
      capture(@git, '-C', root, *arguments).first.strip
    end

    def source(root)
      unless git(root, 'rev-parse', '--show-toplevel') == root &&
             git(root, 'status', '--porcelain=v1', '--untracked-files=all').empty?
        raise Error, "source must be a clean Git root: #{root}"
      end
      sha = git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
      raise Error, "expected a full Git commit: #{root}" unless sha.match?(/\A[0-9a-f]{40}\z/)

      { 'path' => root, 'commit' => sha }
    end

    # The POSIX OS backend writes shell and make wrappers. Restrict its paths
    # to literal characters accepted by those upstream wrappers. Layout-only
    # tools do not impose this additional backend limit.
    def backend_path(value)
      path = PrebuildSDK.path(value)
      raise Error, "unsupported character in POSIX build path: #{path}" unless path.match?(%r{\A[/A-Za-z0-9_.:+-]+\z})

      path
    end

    def output_path(value, boundary)
      path = backend_path(value)
      if @logical_sdk && Inventory.inside?(path, @logical_sdk)
        path = @paths.fetch('sdk') + path.delete_prefix(@logical_sdk)
      end
      raise Error, "output must be inside #{boundary}: #{path}" unless Inventory.inside?(path, boundary)
      relative = path.delete_prefix(boundary + File::SEPARATOR)
      current = boundary
      relative.split(File::SEPARATOR).each do |name|
        current = File.join(current, name)
        info = PrebuildSDK.entry(current)
        raise Error, "output ancestor is not a real directory: #{current}" if info && !info.directory?
      end
      path
    end

    def empty_destination!
      return unless PrebuildSDK.entry(@paths.fetch('destination'))

      PrebuildSDK.directory(@paths.fetch('destination'))
      raise Error, 'destination must be new or empty; active sysroot is never overwritten' unless Dir.empty?(@paths.fetch('destination'))
    end

    def disjoint!(left, right)
      if left == right || Inventory.inside?(left, right) || Inventory.inside?(right, left)
        raise Error, "overlapping input/output paths: #{left} and #{right}"
      end
    end

    def compiler
      prefix = @paths.fetch('compiler')
      triplet = @target.fetch('triplet')
      binaries = {}
      %w[gcc g++ cpp ar as ld nm objcopy objdump ranlib readelf strip].each do |name|
        selected = File.join(prefix, 'bin', triplet + '-' + name)
        path = executable(selected)
        if %w[gcc g++ cpp].include?(name) && !Inventory.inside?(path, prefix)
          raise Error, "compiler frontend escapes prefix: #{path}"
        end
        # Shared Binutils can be selected through aliases in this prefix.
        # Record the alias and its actual provider. Do not search other paths.
        binaries[name] = { 'selected_path' => selected, 'path' => path, 'sha256' => Digest::SHA256.file(path).hexdigest }

      end
      %w[gcc g++].each do |name|
        tool = binaries.fetch(name).fetch('path')
        unless capture(tool, '-dumpfullversion').first.strip == '16.2.0' &&
               capture(tool, '-dumpmachine').first.strip == triplet &&
               capture(tool, '-v').join("\n").match?(/^Thread model: posix$/)
          raise Error, "expected GCC 16.2.0 #{triplet} with POSIX threads: #{tool}"
        end
      end
      %w[cc1 cc1plus collect2 lto1 as ld].each do |name|
        driver = binaries.fetch(name == 'cc1plus' ? 'g++' : 'gcc').fetch('path')
        reported = capture(driver, '-print-prog-name=' + name).first.strip
        reported = File.join(prefix, 'bin', reported) unless Pathname.new(reported).absolute?
        path = executable(reported)
        if %w[as ld].include?(name)
          raise Error, "driver and prefix select different Binutils providers: #{name}" unless path == binaries.fetch(name).fetch('path')
        elsif !Inventory.inside?(path, prefix)
          raise Error, "compiler subprocess escapes prefix: #{path}"
        end
        binaries['resolved-' + name] = { 'reported_path' => reported, 'path' => path, 'sha256' => Digest::SHA256.file(path).hexdigest }

      end
      { 'version' => '16.2.0', 'triplet' => triplet, 'threads' => 'posix', 'binaries' => binaries }
    end

    def verify_runtime
      stdout, = capture(*@runtime_command)
      report = JSON.parse(stdout, object_class: UniqueObject, allow_duplicate_key: false)
      metadata = report.fetch('metadata')
      expected = {
        'OPSYS' => 'NetBSD', 'OS_VERSION' => '11.0', 'GCC_VERSION' => '16.2.0',
        'MACHINE_ARCH' => @target.fetch('arch'), 'GCC_RUNTIME_MACHINE_ARCH' => @target.fetch('arch'),
        'GCC_RUNTIME_TARGET' => @options.fetch('target'), 'GCC_RUNTIME_TRIPLET' => @target.fetch('triplet')
      }
      raise Error, 'runtime target metadata mismatch' unless expected.all? { |key, value| metadata[key] == value }
      raise Error, 'runtime seed provenance is missing' unless metadata.fetch('EMBERBSD_SEED_SOURCE').match?(/\A[0-9a-f]{40}\z/)
      { 'inputs.txt' => 'GCC_RUNTIME_INPUTS_SHA256', 'stage.sha256' => 'GCC_RUNTIME_STAGE_SHA256',
        'stage.symlinks' => 'GCC_RUNTIME_LINKS_SHA256' }.each do |file, key|
        path = File.join(@paths.fetch('runtime-work'), file)
        Inventory.regular(path)
        raise Error, "runtime receipt changed: #{file}" unless Digest::SHA256.file(path).hexdigest == metadata.fetch(key)
      end
      rows = Inventory.runtime(@paths.fetch('runtime-prefix'), @target.fetch('triplet'), @target.fetch('elf'))
      # Tie the actual payload to the exact manifests checked by Ports. A
      # second check alone cannot detect corrupt-copy-restore (ABA) races.
      files = {}; links = {}
      File.foreach(File.join(@paths.fetch('runtime-work'), 'stage.sha256')) do |line|
        match = line.match(%r{\A([0-9a-f]{64})  \./usr/pkg/gcc16/(.+)\n\z})
        raise Error, 'unsupported runtime file manifest' unless match && !files.key?(match[2])
        files[match[2]] = match[1]
      end
      File.foreach(File.join(@paths.fetch('runtime-work'), 'stage.symlinks')) do |line|
        match = line.match(%r{\A\./usr/pkg/gcc16/([^\t\n]+)\t([^\n]+)\n\z})
        raise Error, 'unsupported runtime link manifest' unless match && !links.key?(match[1])
        links[match[1]] = match[2]
      end
      actual_files = rows.select { |_, row| row['kind'] == 'file' }.transform_values { |row| row.fetch('sha256') }
      actual_links = rows.select { |_, row| row['kind'] == 'link' }.transform_values { |row| row.fetch('target') }
      raise Error, 'runtime payload does not match the verified manifests' unless actual_files == files && actual_links == links
      # Catch manifest replacement between the verifier and inventory parsing.
      %w[stage.sha256 stage.symlinks].zip(%w[GCC_RUNTIME_STAGE_SHA256 GCC_RUNTIME_LINKS_SHA256]).each do |file, key|
        raise Error, 'runtime manifest changed during inspection' unless Digest::SHA256.file(File.join(@paths.fetch('runtime-work'), file)).hexdigest == metadata.fetch(key)
      end
      @commands << { 'argv' => @runtime_command, 'scope' => 'runtime-check', 'status' => 'passed' }
      { 'report' => report, 'inventory' => rows, 'root_mode' => File.stat(@paths.fetch('runtime-prefix')).mode & 0o7777 }
    rescue JSON::ParserError, KeyError, TypeError, NoMethodError => e
      raise Error, "malformed runtime verifier result: #{e.message}"
    end

    def inspect_context(path)
      info = PrebuildSDK.entry(path)
      return nil unless info
      PrebuildSDK.directory(path)
      raise Error, "object context is quarantined: #{path}" if PrebuildSDK.entry(File.join(path, QUARANTINE))
      return nil if Dir.empty?(path)


      record = Inventory.read_json(File.join(path, CONTEXT))
      unless record.is_a?(Hash) && record.keys.sort == %w[format signature sources version] &&
             record['format'] == 'emberbsd-sysroot-object-context' && record['version'].instance_of?(Integer) && record['version'] == 1 &&
             record['signature'] == @signature
        raise Error, "unknown or incompatible object context; leave it unchanged: #{path}"
      end
      %w[os ports].each do |name|
        old = record.fetch('sources').fetch(name).fetch('commit')
        raise Error, 'invalid recorded source pin' unless old.match?(/\A[0-9a-f]{40}\z/)
        git(@paths.fetch(name), 'merge-base', '--is-ancestor', old, @sources.fetch(name).fetch('commit'))
      end
      record
    rescue KeyError, TypeError, NoMethodError => e
      raise Error, "invalid object context: #{path}: #{e.message}"
    end

    def preflight
      raise Error, 'the OS build backend requires a POSIX host; use a Linux/WSL2 environment on Windows' if File::ALT_SEPARATOR
      @target = TARGETS[@options.fetch('target')]
      raise Error, 'target must be aarch64 or amd64' unless @target
      raise Error, 'jobs must be a positive integer' unless @options.fetch('jobs').match?(/\A[1-9][0-9]*\z/)
      @jobs = @options.fetch('jobs')
      @git = find_command('git')
      @shell = find_command('sh')
      layout = PrebuildSDK.inspect_layout(@options.fetch('sdk'))
      @logical_sdk = layout.fetch('logical_root')
      @paths = { 'sdk' => backend_path(layout.fetch('canonical_root')) }
      %w[os ports compiler runtime-work gcc-archive pkgsrc].each do |name|
        @paths[name] = backend_path(canonical(@options.fetch(name)))
      end
      %w[os ports compiler runtime-work pkgsrc].each { |name| PrebuildSDK.directory(@paths.fetch(name)) }
      Inventory.regular(@paths.fetch('gcc-archive'))
      sdk = @paths.fetch('sdk')
      @host = RbConfig::CONFIG.fetch('host').gsub(/[^A-Za-z0-9_.+-]/, '_')
      profile = @host + '-' + @options.fetch('target')
      @paths['destination'] = output_path(@options.fetch('destination', File.join(sdk, 'sysroot')), sdk)
      %w[objects tools].zip(%w[os tools]).each do |name, directory|
        @paths[name] = output_path(@options.fetch(name, File.join(sdk, 'obj', directory, profile)), File.join(sdk, 'obj'))
      end
      empty_destination!
      %w[obj packages receipts].each { |name| disjoint!(@paths.fetch('destination'), File.join(sdk, name)) }
      %w[destination objects tools].combination(2) { |left, right| disjoint!(@paths.fetch(left), @paths.fetch(right)) }
      %w[os ports compiler runtime-work gcc-archive pkgsrc].each do |input|
        %w[destination objects tools].each { |output| disjoint!(@paths.fetch(input), @paths.fetch(output)) }
      end
      # Do not accept a partial checkout as a complete OS source tree.
      %w[include lib libexec/ld.elf_so tools share/mk sys distrib etc].each do |name|
        PrebuildSDK.directory(File.join(@paths.fetch('os'), name))
      end
      %w[build.sh Makefile libexec/ld.elf_so/Makefile share/mk/bsd.own.mk].each do |name|
        Inventory.regular(File.join(@paths.fetch('os'), name))
      end
      Inventory.regular(File.join(@paths.fetch('ports'), VERIFIER))
      @sources = %w[os ports].to_h { |name| [name, source(@paths.fetch(name))] }
      git(@paths.fetch('os'), 'ls-files', '--error-unmatch', 'build.sh', 'Makefile', 'libexec/ld.elf_so/Makefile', 'share/mk/bsd.own.mk')
      git(@paths.fetch('ports'), 'ls-files', '--error-unmatch', VERIFIER)
      prefix = File.join(@paths.fetch('runtime-work'), 'stage/usr/pkg/gcc16')
      raise Error, 'runtime prefix must be the real WORK/stage/usr/pkg/gcc16 tree' unless canonical(prefix) == prefix
      @paths['runtime-prefix'] = prefix
      @runtime_command = [RbConfig.ruby, File.join(@paths.fetch('ports'), VERIFIER), 'check',
                          @paths.fetch('runtime-work'), @paths.fetch('gcc-archive'), @paths.fetch('pkgsrc')]
      @compiler = compiler
      @runtime = verify_runtime
      @host_tools = %w[cc c++].to_h do |name|
        path = find_command(name)
        [name, { 'path' => path, 'sha256' => Digest::SHA256.file(path).hexdigest,
                 'version' => capture(path, '--version').join.strip }]
      end
      @flags = FLAGS + ["EXTERNAL_TOOLCHAIN=#{@paths.fetch('compiler')}",
                        "EMBER_EXTERNAL_GCC_RUNTIME=#{prefix}",
                        "HOST_CC=#{@host_tools.fetch('cc').fetch('path')}",
                        "HOST_CXX=#{@host_tools.fetch('c++').fetch('path')}"]
      @signature = { 'host' => @host, 'target' => @target, 'compiler' => @compiler,
                     'runtime' => @runtime, 'runtime_prefix' => prefix, 'flags' => @flags,
                     'host_tools' => @host_tools, 'environment' => @environment,
                     'objects' => @paths.fetch('objects'), 'tools' => @paths.fetch('tools') }
      @contexts = %w[objects tools].to_h { |name| [name, inspect_context(@paths.fetch(name))] }
      @build_command = [@shell, File.join(@paths.fetch('os'), 'build.sh'), '-U', '-u', '-j' + @jobs,
                        '-m', @target.fetch('machine'), '-a', @target.fetch('arch'),
                        '-O', @paths.fetch('objects'), '-T', @paths.fetch('tools'), '-D', @paths.fetch('destination')]
      (@flags + ["BUILDINFO=EmberBSD:#{@sources.fetch('os').fetch('commit')}"]).each { |flag| @build_command.concat(['-V', flag]) }
      @build_command.concat(%w[tools libs])
      @loader_command = [File.join(@paths.fetch('tools'), 'bin', 'nbmake-' + @target.fetch('machine')),
                         '-C', File.join(@paths.fetch('os'), 'libexec/ld.elf_so'), '-j' + @jobs, 'dependall']
      @install_command = @loader_command[0...-1] + ['install']
      @receipt = { 'format' => 'emberbsd-sysroot-build', 'version' => 1, 'scope' => 'sysroot-build',
                   'qualification' => 'not-built', 'execution_acceptance' => 'not-run',
                   'paths' => @paths, 'sources' => @sources, 'previous_contexts' => @contexts,
                   'signature' => @signature, 'commands' => @commands,
                   'plan' => [@runtime_command, @build_command, @loader_command, @install_command, @runtime_command, @runtime_command],
                   'working_directory' => @paths.fetch('os'),
                   'copy' => { 'source' => prefix, 'destination' => File.join(@paths.fetch('destination'), 'usr/pkg/gcc16'), 'verification' => 'exact pre/copy/post inventory' },
                   'not_checked' => ['target execution', 'ABI compatibility', 'SDK release'] }
      @receipt
    end

    def mkdirs(path, boundary)
      relative = path.delete_prefix(boundary + File::SEPARATOR)
      current = boundary
      relative.split(File::SEPARATOR).each do |name|
        current = File.join(current, name)
        begin
          Dir.mkdir(current)
        rescue Errno::EEXIST
          PrebuildSDK.directory(current)
        end
      end
    end

    def exclusive_file(path, &block)
      File.open(path, File::WRONLY | File::CREAT | File::EXCL, 0o644, &block)
    end

    def save_receipt
      @receipt_file.rewind
      @receipt_file.write(JSON.pretty_generate(@receipt) + "\n")
      @receipt_file.truncate(@receipt_file.pos)
      @receipt_file.flush
      @receipt_file.fsync
    end

    def lock_contexts
      %w[objects tools].map { |name| @paths.fetch(name) }.sort.each do |path|
        lock = File.join(@paths.fetch('sdk'), 'obj', '.prebuild-sdk-lock-' + Digest::SHA256.hexdigest(path))
        info = PrebuildSDK.entry(lock)
        raise Error, "invalid object lock: #{lock}" if info && !info.file?
        flags = File::RDWR | File::CREAT
        flags |= File::NOFOLLOW if File.const_defined?(:NOFOLLOW)
        handle = File.open(lock, flags, 0o644)
        @locks << handle
        raise Error, "object context is busy: #{path}" unless handle.flock(File::LOCK_EX | File::LOCK_NB)
      end
    end

    def prepare_contexts
      record = { 'format' => 'emberbsd-sysroot-object-context', 'version' => 1,
                 'signature' => @signature, 'sources' => @sources }
      %w[objects tools].each do |name|
        path = @paths.fetch(name)
        current = inspect_context(path)
        raise Error, 'object context changed after preflight' unless current == @contexts.fetch(name)
      end
      %w[objects tools].each do |name|
        path = @paths.fetch(name)
        mkdirs(path, File.join(@paths.fetch('sdk'), 'obj'))
        context = File.join(path, CONTEXT)
        if @contexts.fetch(name)
          # This file is builder-owned and validated under the context lock.
          flags = File::WRONLY
          flags |= File::NOFOLLOW if File.const_defined?(:NOFOLLOW)
          File.open(context, flags) do |file|
            file.write(JSON.pretty_generate(record) + "\n")
            file.truncate(file.pos)
            file.flush
            file.fsync
          end
        else
          exclusive_file(context) do |file|
            file.write(JSON.pretty_generate(record) + "\n")
            file.flush
            file.fsync
          end
        end
      end
    end

    def dispatch(argv)
      status = nil
      cleanup_attempted = false
      row = { 'argv' => argv, 'status' => 'running' }
      @commands << row
      save_receipt
      interrupted!
      pid = Process.spawn(@environment, *argv, chdir: @paths.fetch('os'), unsetenv_others: true, out: @log, err: @log, pgroup: true)
      loop do
        interrupted!
        completed = Process.wait2(pid, Process::WNOHANG)
        if completed
          _, status = completed
          break
        end
        sleep 0.05
      end
      row['exit_status'] = status.exitstatus
      if group_alive?(pid)
        row['status'] = 'failed'
        row['error'] = 'producer left live descendants in its process group'
        cleanup_attempted = true
        stop_group(pid)
        save_receipt
        raise Error, row.fetch('error')
      end
      row['signal'] = status.termsig if status.signaled?
      row['status'] = status.success? ? 'passed' : 'failed'
      save_receipt
      raise Error, "build command failed; inspect #{File.join(@paths.fetch('destination'), LOG)}" unless status.success?
    rescue Interrupt
      # Keep context locks until this invocation's process group has stopped.
      # Repeated signals must not abandon an active child build.
      %w[INT TERM HUP].each { |signal| Signal.trap(signal, 'IGNORE') }
      cleanup_attempted = true
      stop_group(pid) if pid
      row['status'] = 'interrupted'
      raise
    ensure
      # Other exceptions must not abandon a live command group either.
      stop_group(pid) if pid && !cleanup_attempted && group_alive?(pid)
    end

    def signal_group(pid, signal)
      Process.kill(signal, -pid)
    rescue Errno::ESRCH
      # The owned process group has already disappeared.
    end

    def group_alive?(pid)
      Process.kill(0, -pid)
      true
    rescue Errno::ESRCH
      false
    end

    def drain_group(pid, seconds)
      deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + seconds
      loop do
        begin
          Process.wait2(pid, Process::WNOHANG)
        rescue Errno::ECHILD
          # The leader was reaped. Descendants can still be in its group.
        end
        return true unless group_alive?(pid)
        return false if Process.clock_gettime(Process::CLOCK_MONOTONIC) >= deadline
        sleep 0.05
      end
    end

    def quarantine_contexts(pid)
      %w[objects tools].each do |name|
        path = File.join(@paths.fetch(name), QUARANTINE)
        next if PrebuildSDK.entry(path)
        exclusive_file(path) do |file|
          file.write(JSON.generate('reason' => 'owned process group did not stop', 'pgid' => pid) + "\n")
          file.flush
          file.fsync
        end
      end
    end

    def stop_group(pid)
      signal_group(pid, 'TERM')
      return if drain_group(pid, 2)
      signal_group(pid, 'KILL')
      return if drain_group(pid, 10)
      quarantine_contexts(pid)
      raise Error, 'owned process group did not disappear; object contexts are quarantined'
    rescue SystemCallError => e
      quarantine_contexts(pid)
      raise Error, "cannot stop the owned process group; object contexts are quarantined: #{e.message}"
    end

    def copy_runtime
      # Recheck before copying. Compare destination bytes with the accepted
      # inventory, not only with the source after the copy.
      raise Error, 'runtime changed since preflight' unless verify_runtime == @runtime
      source = @paths.fetch('runtime-prefix')
      destination = File.join(@paths.fetch('destination'), 'usr/pkg/gcc16')
      mkdirs(File.dirname(destination), @paths.fetch('destination'))
      Dir.mkdir(destination)
      rows = @runtime.fetch('inventory')
      rows.each do |relative, row|
        interrupted!
        from = File.join(source, relative)
        to = File.join(destination, relative)
        case row.fetch('kind')
        when 'directory' then Dir.mkdir(to)
        when 'link' then File.symlink(row.fetch('target'), to)
        when 'file'
          File.open(to, File::WRONLY | File::CREAT | File::EXCL, row.fetch('mode')) do |output|
            File.open(from, 'rb') { |input| IO.copy_stream(input, output) }
          end
          File.chmod(row.fetch('mode'), to)
        end
      end
      rows.to_a.reverse_each do |relative, row|
        path = File.join(destination, relative)
        File.chmod(row.fetch('mode'), path) if row['kind'] == 'directory'
        if row['kind'] == 'link' && (File.lstat(path).mode & 0o7777) != row.fetch('mode')
          begin
            File.lchmod(row.fetch('mode'), path)
          rescue NotImplementedError
            raise Error, 'this host cannot preserve the audited symbolic-link mode'
          end
        end
      end
      File.chmod(@runtime.fetch('root_mode'), destination)
      copied = Inventory.runtime(destination, @target.fetch('triplet'), @target.fetch('elf'))
      raise Error, 'copied runtime differs from the verified payload' unless copied == rows
      raise Error, 'runtime inputs changed during copy' unless verify_runtime == @runtime
    end

    def interrupted!
      raise Interrupt, "received #{@interrupted_signal}" if @interrupted_signal
    end

    def run
      preflight
      return @receipt if @options['plan']

      @signal_handlers = %w[INT TERM HUP].to_h do |signal|
        [signal, Signal.trap(signal) { @interrupted_signal = signal }]
      end
      interrupted!
      lock_contexts
      empty_destination!
      mkdirs(@paths.fetch('destination'), @paths.fetch('sdk'))
      marker = File.join(@paths.fetch('destination'), MARKER)
      @marker = exclusive_file(marker)
      @marker.write("This sysroot build is incomplete. Do not accept this payload.\n")
      @marker.flush
      raise Error, 'destination changed during claim' unless Dir.children(@paths.fetch('destination')) == [MARKER]
      @receipt_file = exclusive_file(File.join(@paths.fetch('destination'), RECEIPT))
      @log = exclusive_file(File.join(@paths.fetch('destination'), LOG))
      @receipt['qualification'] = 'incomplete'
      save_receipt
      prepare_contexts
      dispatch(@build_command)
      dispatch(@loader_command)
      dispatch(@install_command)
      copy_runtime
      @sources.each { |name, expected| raise Error, "#{name} source changed during build" unless source(@paths.fetch(name)) == expected }
      raise Error, 'compiler changed during build' unless compiler == @compiler
      @receipt['outputs'] = Inventory.sysroot(@paths.fetch('destination'), @target.fetch('elf'), exclude: [RECEIPT, MARKER, LOG])
      interrupted!
      @receipt['qualification'] = 'built-and-inspected'
      save_receipt
      File.unlink(marker)
      @receipt
    rescue StandardError, Interrupt => e
      if @receipt_file
        @receipt['qualification'] = 'incomplete'
        @receipt['error'] = e.message
        save_receipt
      end
      raise
    ensure
      [@marker, @receipt_file, @log, *@locks].compact.each(&:close)
      @signal_handlers&.each { |signal, previous| Signal.trap(signal, previous) }
    end
  end
end
