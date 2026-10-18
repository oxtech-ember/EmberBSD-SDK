#!/usr/bin/env ruby
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted real-filesystem regression tests.

require 'digest'
require 'fileutils'
require 'open3'
require 'rbconfig'
require 'tmpdir'
require_relative '../lib/prebuild_sdk'

module LayoutTests
  module_function

  def assert(condition, message = 'assertion failed')
    raise message unless condition
  end

  def reject
    yield
    raise 'expected a layout error'
  rescue PrebuildSDK::Error
    true
  end

  def snapshot(root)
    return nil unless File.exist?(root) || File.symlink?(root)

    paths = [root]
    paths.concat(Dir.glob(File.join(root, '**', '*'), File::FNM_DOTMATCH)) unless File.symlink?(root)
    paths.reject { |p| ['.', '..'].include?(File.basename(p)) }.sort.map do |p|
      s = File.lstat(p)
      content = s.symlink? ? File.readlink(p) : s.file? ? Digest::SHA256.file(p).hexdigest : nil
      [p.delete_prefix(root), s.dev, s.ino, s.mode, s.size, s.mtime.to_r, content]
    end
  end

  def legacy(root)
    Dir.mkdir(root)
    PrebuildSDK::REQUIRED.each { |name| Dir.mkdir(File.join(root, name)) }
    File.binwrite(File.join(root, 'sysroot', 'empty'), '')
    File.binwrite(File.join(root, 'packages', 'payload'), "payload\x00\xff".b)
    File.symlink('../packages/payload', File.join(root, 'obj', 'payload-link'))
    root
  end

  def temporary_method(object, name, replacement)
    singleton = object.singleton_class
    owned = singleton.instance_methods(false).include?(name)
    original = object.method(name)
    singleton.send(:remove_method, name) if owned
    object.define_singleton_method(name, &replacement)
    installed = true
    yield
  ensure
    singleton.send(:remove_method, name) if installed
    object.define_singleton_method(name, original) if owned && original
  end

  def cli(kind, *args)
    script = File.expand_path("../scripts/#{kind}-prebuild-sdk.rb", __dir__)
    Open3.capture3(RbConfig.ruby, script, *args)
  end

  def run
    cases = 0
    Dir.mktmpdir('prebuild-sdk-test-', ENV.fetch('TMPDIR', Dir.tmpdir)) do |base|
      base = File.realpath(base)
      test = lambda do |name, &body|
        Dir.mktmpdir('case-', base) { |dir| body.call(dir) }
        cases += 1
        puts "PASS: #{name}"
      end

      test.call('create managed layout and inspect without writes') do |dir|
        root = File.join(dir, 'sdk')
        result = PrebuildSDK.create(root)
        assert(result['structural_status'] == 'managed')
        assert(result['qualification'] == 'not-assessed')
        assert(result['canonical_root'] == File.realpath(root))
        assert(JSON.parse(File.read(File.join(root, PrebuildSDK::MANIFEST_NAME))) == PrebuildSDK::MANIFEST)
        assert(PrebuildSDK::DIRECTORIES.all? { |n| Dir.empty?(File.join(root, n)) })
        before = snapshot(root)
        assert(PrebuildSDK.inspect_layout(root) == result)
        reject { PrebuildSDK.create(root) }
        assert(snapshot(root) == before)
      end

      test.call('adopt legacy payload and repeat alias without writes') do |dir|
        source = legacy(File.join(dir, 'existing'))
        root = File.join(dir, 'sdk')
        before = snapshot(source)
        result = PrebuildSDK.create(root, adopt: source)
        assert(result['structural_status'] == 'adoptable-existing')
        assert(result['manifest'].nil? && result['directories']['receipts'].nil?)
        assert(result['canonical_root'] == source && File.symlink?(root))
        alias_before = snapshot(root)
        assert(PrebuildSDK.create(root, adopt: source) == result)
        assert(snapshot(source) == before && snapshot(root) == alias_before)
      end

      test.call('adopt managed layout and an existing relative alias') do |dir|
        source = File.join(dir, 'existing')
        PrebuildSDK.create(source)
        root = File.join(dir, 'sdk')
        File.symlink('existing', root)
        before = snapshot(source)
        alias_before = snapshot(root)
        assert(PrebuildSDK.create(root, adopt: source)['structural_status'] == 'managed')
        assert(snapshot(source) == before && snapshot(root) == alias_before)
      end

      %w[file directory symlink dangling].each do |kind|
        test.call("destination collision #{kind}") do |dir|
          source = legacy(File.join(dir, 'source'))
          root = File.join(dir, 'sdk')
          case kind
          when 'file' then File.write(root, 'keep')
          when 'directory' then Dir.mkdir(root)
          when 'symlink' then File.symlink(source, root)
          when 'dangling' then File.symlink(File.join(dir, 'missing'), root)
          end
          before = snapshot(root)
          payload = snapshot(source)
          reject { PrebuildSDK.create(root) }
          reject { PrebuildSDK.create(root, adopt: source) } unless kind == 'symlink'
          assert(snapshot(root) == before && snapshot(source) == payload)
        end
      end

      PrebuildSDK::REQUIRED.each do |name|
        %w[missing file symlink dangling].each do |kind|
          test.call("reject #{kind} child #{name}") do |dir|
            source = legacy(File.join(dir, 'source'))
            child = File.join(source, name)
            FileUtils.remove_entry(child)
            case kind
            when 'file' then File.write(child, 'keep')
            when 'symlink' then File.symlink(dir, child)
            when 'dangling' then File.symlink(File.join(dir, 'missing'), child)
            end
            before = snapshot(source)
            reject { PrebuildSDK.inspect_layout(source) }
            reject { PrebuildSDK.create(File.join(dir, 'sdk'), adopt: source) }
            assert(!File.exist?(File.join(dir, 'sdk')) && snapshot(source) == before)
          end
        end
      end

      malformed = {
        'empty' => '', 'broken JSON' => '{', 'null' => 'null', 'array' => '[]',
        'wrong version' => JSON.generate(PrebuildSDK::MANIFEST.merge('version' => 2)),
        'floating version' => JSON.generate(PrebuildSDK::MANIFEST.merge('version' => 1.0)),
        'unknown key' => JSON.generate(PrebuildSDK::MANIFEST.merge('qualified' => true)),
        'wrong directory' => JSON.generate(PrebuildSDK::MANIFEST.merge('directories' => ['other'])),
        'duplicate key' => JSON.generate(PrebuildSDK::MANIFEST).sub('"version":1', '"version":0,"version":1'),
        'oversized' => ' ' * (PrebuildSDK::MAX_MANIFEST + 1),
        'deep JSON' => '[' * 10 + '0' + ']' * 10
      }
      malformed.each do |name, text|
        test.call("reject manifest #{name}") do |dir|
          source = File.join(dir, 'source')
          PrebuildSDK.create(source)
          File.binwrite(File.join(source, PrebuildSDK::MANIFEST_NAME), text)
          before = snapshot(source)
          reject { PrebuildSDK.inspect_layout(source) }
          reject { PrebuildSDK.create(File.join(dir, 'sdk'), adopt: source) }
          assert(snapshot(source) == before)
        end
      end

      %w[directory symlink dangling].each do |kind|
        test.call("reject manifest #{kind}") do |dir|
          source = legacy(File.join(dir, 'source'))
          manifest = File.join(source, PrebuildSDK::MANIFEST_NAME)
          if kind == 'directory'
            Dir.mkdir(manifest)
          else
            target = File.join(dir, 'metadata')
            File.write(target, JSON.generate(PrebuildSDK::MANIFEST)) if kind == 'symlink'
            File.symlink(target, manifest)
          end
          before = snapshot(source)
          reject { PrebuildSDK.inspect_layout(source) }
          assert(snapshot(source) == before)
        end
      end

      test.call('reject missing managed receipts and linked legacy receipts') do |dir|
        source = File.join(dir, 'managed')
        PrebuildSDK.create(source)
        Dir.rmdir(File.join(source, 'receipts'))
        reject { PrebuildSDK.inspect_layout(source) }
        other = legacy(File.join(dir, 'legacy'))
        File.symlink(dir, File.join(other, 'receipts'))
        reject { PrebuildSDK.inspect_layout(other) }
      end

      test.call('reject dangling root and symlink adoption source') do |dir|
        missing = File.join(dir, 'missing')
        link = File.join(dir, 'link')
        File.symlink(missing, link)
        reject { PrebuildSDK.inspect_layout(link) }
        reject { PrebuildSDK.create(File.join(dir, 'sdk'), adopt: link) }
        legacy(missing)
        reject { PrebuildSDK.create(File.join(dir, 'sdk'), adopt: link) }
      end

      test.call('reject a preexisting alias to a different valid workspace') do |dir|
        a = legacy(File.join(dir, 'a'))
        b = legacy(File.join(dir, 'b'))
        link = File.join(dir, 'sdk')
        File.symlink(a, link)
        before = snapshot(link)
        reject { PrebuildSDK.create(link, adopt: b) }
        assert(snapshot(link) == before)
      end

      [1, 2, 3, 4].each do |failure|
        test.call("failed mkdir #{failure} retains incomplete marker and foreign file") do |dir|
          root = File.join(dir, 'sdk')
          original = Dir.method(:mkdir)
          count = 0
          replacement = proc do |value, *args|
            if value.start_with?(root + '/')
              count += 1
              if count == failure
                File.write(File.join(root, 'foreign'), 'do not remove')
                raise Errno::EIO, 'injected mkdir fault'
              end
            end
            original.call(value, *args)
          end
          temporary_method(Dir, :mkdir, replacement) { reject { PrebuildSDK.create(root) } }
          assert(File.read(File.join(root, 'foreign')) == 'do not remove')
          assert(File.file?(File.join(root, PrebuildSDK::INCOMPLETE_NAME)))
          reject { PrebuildSDK.inspect_layout(root) }
          reject { PrebuildSDK.create(File.join(dir, 'alias'), adopt: root) }
        end
      end

      test.call('atomic symlink collision preserves racing file') do |dir|
        source = legacy(File.join(dir, 'source'))
        root = File.join(dir, 'sdk')
        original = File.method(:symlink)
        replacement = proc do |from, to|
          File.write(to, 'racing file')
          original.call(from, to)
        end
        before = snapshot(source)
        temporary_method(File, :symlink, replacement) { reject { PrebuildSDK.create(root, adopt: source) } }
        assert(File.read(root) == 'racing file' && snapshot(source) == before)
      end

      test.call('adoption never treats the physical root as its own alias') do |dir|
        source = legacy(File.join(dir, 'source'))
        before = snapshot(source)
        reject { PrebuildSDK.create(source, adopt: source) }
        assert(snapshot(source) == before)
      end

      test.call('atomic mkdir collision preserves racing file') do |dir|
        root = File.join(dir, 'sdk')
        original = Dir.method(:mkdir)
        replacement = proc do |value, *args|
          File.write(value, 'racing file') if value == root
          original.call(value, *args)
        end
        temporary_method(Dir, :mkdir, replacement) { reject { PrebuildSDK.create(root) } }
        assert(File.read(root) == 'racing file')
      end

      %w[manifest marker].each do |phase|
        test.call("late #{phase} failure cannot look like a usable layout") do |dir|
          root = File.join(dir, 'sdk')
          operation = phase == 'manifest' ? :open : :unlink
          filename = phase == 'manifest' ? PrebuildSDK::MANIFEST_NAME : PrebuildSDK::INCOMPLETE_NAME
          original = File.method(operation)
          replacement = proc do |value, *args, &block|
            raise Errno::EIO, 'injected late failure' if value == File.join(root, filename)

            original.call(value, *args, &block)
          end
          temporary_method(File, operation, replacement) { reject { PrebuildSDK.create(root) } }
          assert(File.file?(File.join(root, PrebuildSDK::INCOMPLETE_NAME)))
          reject { PrebuildSDK.inspect_layout(root) }
          reject { PrebuildSDK.create(File.join(dir, 'alias'), adopt: root) }
        end
      end

      test.call('unsupported symlink reports an error without a copy fallback') do |dir|
        source = legacy(File.join(dir, 'source'))
        root = File.join(dir, 'sdk')
        before = snapshot(source)
        replacement = proc { |*_args| raise NotImplementedError, 'host has no symlinks' }
        temporary_method(File, :symlink, replacement) { reject { PrebuildSDK.create(root, adopt: source) } }
        assert(snapshot(source) == before && !File.exist?(root))
      end

      test.call('reject unsafe build paths before creation') do |dir|
        ['', "space name", "tab\tname", "newline\nname", "nul\x00name"].each do |name|
          reject { PrebuildSDK.create(name.empty? ? name : dir + '/' + name) }
        end
        parent = File.join(dir, 'space parent')
        Dir.mkdir(parent)
        alias_parent = File.join(dir, 'parent')
        File.symlink(parent, alias_parent)
        reject { PrebuildSDK.create(File.join(alias_parent, 'sdk')) }
        assert(Dir.empty?(parent))
      end

      test.call('CLI argv transport, JSON, usage and error exits') do |dir|
        root = File.join(dir, 'sdk;$(literal)`quote`')
        out, err, status = cli('create', root)
        assert(status.success? && err.empty?, err)
        assert(JSON.parse(out)['logical_root'] == root)
        out, err, status = cli('inspect', root)
        assert(status.success? && err.empty? && JSON.parse(out)['qualification'] == 'not-assessed')
        out, err, status = cli('create', root)
        assert(!status.success? && out.empty? && err.include?('error:'))
        out, err, status = cli('inspect')
        assert(status.exitstatus == 2 && out.empty? && err.include?('Usage:'))
        out, err, status = cli('create', root, '--unknown', root)
        assert(status.exitstatus == 2 && out.empty? && err.include?('Usage:'))
        assert(Dir.children(dir) == [File.basename(root)])
      end
    end
    puts "PASS: #{cases} real-filesystem Prebuild SDK cases; no payload qualification"
  end
end

LayoutTests.run
