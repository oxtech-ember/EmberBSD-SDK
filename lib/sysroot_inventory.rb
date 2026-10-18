# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted SDK payload inspection.

require 'digest'
require 'find'
require 'pathname'
require_relative 'prebuild_sdk'

module PrebuildSDK
  module Inventory
    module_function

    def inside?(path, root)
      path.start_with?(root + File::SEPARATOR)
    end

    def regular(path)
      info = File.lstat(path)
      raise Error, "expected a regular file: #{path}" unless info.file?

      info
    end

    def read_json(path)
      regular(path)
      JSON.parse(File.binread(path), object_class: UniqueObject,
                 allow_duplicate_key: false, max_nesting: 32, allow_nan: false)
    rescue JSON::ParserError, JSON::NestingError => e
      raise Error, "invalid JSON: #{path}: #{e.message}"
    end

    def elf(path, machine, required_type = nil)
      elf_header(File.binread(path, 64) || ''.b, path, machine, required_type)
    end

    def elf_header(data, path, machine, required_type = nil)
      return nil unless data.start_with?("\x7fELF".b)

      unless data.bytesize == 64 && data.getbyte(4) == 2 && data.getbyte(5) == 1 &&
             data.getbyte(6) == 1 && data.byteslice(18, 2).unpack1('v') == machine &&
             data.byteslice(20, 4).unpack1('V') == 1 && data.byteslice(52, 2).unpack1('v') == 64
        raise Error, "expected ELF64 little-endian target header: #{path}"
      end
      type = data.byteslice(16, 2).unpack1('v')
      raise Error, "wrong ELF type: #{path}" if required_type && type != required_type

      { 'class' => 64, 'endian' => 'little', 'machine' => machine, 'type' => type }
    end

    # Read normal GNU/BSD ar members in place. Never extract an archive.
    # Thin archives depend on external paths and are not SDK payloads.
    def archive(path, machine)
      count = 0
      File.open(path, 'rb') do |file|
        raise Error, "expected a normal static archive: #{path}" unless file.read(8) == "!<arch>\n"
        while file.pos < file.size
          header = file.read(60)
          unless header && header.bytesize == 60 && header.byteslice(58, 2) == "`\n" &&
                 header.byteslice(48, 10).match?(/\A[0-9]+ *\z/)
            raise Error, "invalid archive header: #{path}"
          end
          size = header.byteslice(48, 10).to_i
          finish = file.pos + size
          raise Error, "truncated archive member: #{path}" if finish > file.size
          name = header.byteslice(0, 16).strip
          if name.start_with?('#1/')
            raise Error, "invalid BSD archive name: #{path}" unless name.match?(/\A#1\/[0-9]+\z/)
            length = name.delete_prefix('#1/').to_i
            raise Error, "invalid BSD archive name size: #{path}" if length > size || length > 4096
            name = file.read(length).delete_suffix("\0").delete("\0")
          end
          unless %w[/ // /SYM64/ __.SYMDEF __.SYMDEF\ SORTED __.SYMDEF_64 __.SYMDEF_64\ SORTED].include?(name.delete_suffix('/')) || %w[/ // /SYM64/].include?(name)
            header_bytes = file.read([finish - file.pos, 64].min) || ''.b
            raise Error, "non-ELF archive member: #{path}: #{name}" unless elf_header(header_bytes, path + ':' + name, machine, 1)
            count += 1
          end
          file.seek(finish)
          if size.odd?
            raise Error, "missing archive padding: #{path}" unless file.read(1) == "\n"
          end
        end
      end
      { 'elf_members' => count, 'machine' => machine }
    end

    # lstat and Find do not traverse directory symlinks. All link targets must
    # resolve inside this payload. Empty files are valid non-ELF payloads.
    def tree(root, machine: nil, exclude: [])
      PrebuildSDK.directory(root)
      rows = {}
      Find.find(root) do |path|
        next if path == root

        relative = path.delete_prefix(root + File::SEPARATOR)
        next if exclude.include?(relative)
        raise Error, "control character in payload path: #{path}" if relative.match?(/[[:cntrl:]]/)

        stat = File.lstat(path)
        row = { 'mode' => stat.mode & 0o7777 }
        if stat.symlink?
          target = File.readlink(path)
          if Pathname.new(target).absolute? || target.match?(/[[:cntrl:]]/) || !inside?(File.realpath(path), root)
            raise Error, "absolute or escaped payload link: #{path}"
          end
          row.merge!('kind' => 'link', 'target' => target)
        elsif stat.directory?
          row['kind'] = 'directory'
        elsif stat.file?
          row.merge!('kind' => 'file', 'size' => stat.size, 'sha256' => Digest::SHA256.file(path).hexdigest)
          row['elf'] = elf(path, machine) if machine
          row.delete('elf') unless row['elf']
          row['archive'] = archive(path, machine) if machine && relative.end_with?('.a')
        else
          raise Error, "special file in payload: #{path}"
        end
        after = File.lstat(path)
        unless [stat.dev, stat.ino, stat.mode, stat.size, stat.mtime, stat.ctime] ==
               [after.dev, after.ino, after.mode, after.size, after.mtime, after.ctime]
          raise Error, "payload changed during inspection: #{path}"
        end
        rows[relative] = row
      end
      rows.sort.to_h
    end

    def required_file(root, relative)
      path = File.join(root, relative)
      resolved = File.realpath(path)
      raise Error, "required path escapes payload: #{relative}" unless inside?(resolved, root)
      regular(resolved)
      raise Error, "empty required file: #{relative}" if File.size(resolved).zero?

      resolved
    end

    def required_elf(root, relative, machine, type)
      path = required_file(root, relative)
      raise Error, "required ELF missing: #{relative}" unless elf(path, machine, type)
    end

    def runtime(root, triplet, machine)
      rows = tree(root, machine: machine)
      %W[include/c++/vector include/c++/#{triplet}/bits/c++config.h include/c++/backward/backward_warning.h].each do |path|
        required_file(root, path)
      end
      %w[crtbegin.o crtbeginS.o crtend.o crtendS.o].each do |name|
        required_elf(root, "lib/gcc/#{triplet}/16.2.0/#{name}", machine, 1)
      end
      %W[lib/gcc/#{triplet}/16.2.0/libgcc.a lib/gcc/#{triplet}/16.2.0/libgcc_eh.a lib/libstdc++.a lib/libsupc++.a].each do |path|
        file = required_file(root, path)
        raise Error, "empty developer archive: #{path}" if archive(file, machine).fetch('elf_members').zero?
      end
      %w[lib/libgcc_s.so.1 lib/libstdc++.so.7 lib/libstdc++.so].each do |path|
        required_elf(root, path, machine, 3)
      end
      rows
    end

    def sysroot(root, machine, exclude: [])
      rows = tree(root, machine: machine, exclude: exclude)
      %w[usr/include/stdio.h usr/include/sys/types.h usr/include/math.h usr/include/pthread.h].each do |path|
        required_file(root, path)
      end
      %w[crt0.o crti.o crtn.o].each { |name| required_elf(root, 'usr/lib/' + name, machine, 1) }
      %w[libc.so libm.so libpthread.so].each { |name| required_elf(root, 'usr/lib/' + name, machine, 3) }
      required_elf(root, 'libexec/ld.elf_so', machine, 3)
      rows
    end
  end
end
