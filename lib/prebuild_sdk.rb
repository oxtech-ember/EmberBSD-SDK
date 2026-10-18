# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted workspace layout tools.

require 'json'

module PrebuildSDK
  class Error < StandardError; end

  # JSON objects must not silently replace duplicate metadata keys.
  class UniqueObject < Hash
    def []=(key, value)
      raise Error, "duplicate metadata key: #{key}" if key?(key)

      super
    end
  end

  REQUIRED = %w[obj sysroot packages].freeze
  DIRECTORIES = (REQUIRED + ['receipts']).freeze
  MANIFEST_NAME = '.prebuild-sdk.json'.freeze
  INCOMPLETE_NAME = '.prebuild-sdk.incomplete'.freeze
  MAX_MANIFEST = 4096
  MANIFEST = {
    'format' => 'emberbsd-prebuild-sdk-layout',
    'version' => 1,
    'scope' => 'workspace-layout',
    'directories' => DIRECTORIES
  }.freeze

  module_function

  def path(value)
    unless value.is_a?(String) && value.valid_encoding? && !value.empty? &&
           !value.match?(/[[:space:]]|[[:cntrl:]]/)
      raise Error, 'build paths must be nonempty and contain no whitespace or control characters'
    end

    expanded = File.expand_path(value)
    raise Error, 'expanded build path contains whitespace or control characters' if expanded.match?(/[[:space:]]|[[:cntrl:]]/)

    expanded
  end

  def entry(value)
    File.lstat(value)
  rescue Errno::ENOENT
    nil
  end

  def directory(value)
    info = entry(value)
    raise Error, "expected a real directory: #{value}" unless info && info.directory?

    info
  end

  def read_manifest(value)
    info = entry(value)
    raise Error, "expected a regular manifest: #{value}" unless info && info.file?

    flags = File::RDONLY
    flags |= File::NOFOLLOW if File.const_defined?(:NOFOLLOW)
    flags |= File::NONBLOCK if File.const_defined?(:NONBLOCK)
    text = File.open(value, flags) do |file|
      stat = file.stat
      unless stat.file? && [stat.dev, stat.ino] == [info.dev, info.ino]
        raise Error, "manifest changed during inspection: #{value}"
      end
      file.read(MAX_MANIFEST + 1) || ''.b
    end
    after = entry(value)
    unless after && after.file? && [after.dev, after.ino] == [info.dev, info.ino]
      raise Error, "manifest changed during inspection: #{value}"
    end
    raise Error, 'manifest exceeds the size limit' if text.bytesize > MAX_MANIFEST

    parsed = JSON.parse(text, object_class: UniqueObject, max_nesting: 8,
                        allow_nan: false, allow_duplicate_key: false)
    unless parsed.is_a?(Hash) && parsed['version'].instance_of?(Integer) && parsed == MANIFEST
      raise Error, "unsupported or malformed layout manifest: #{value}"
    end
  rescue JSON::ParserError, JSON::NestingError => e
    raise Error, "invalid layout JSON: #{e.message}"
  end

  def inspect_layout(root)
    logical = path(root)
    canonical = path(File.realpath(logical))
    identity = directory(canonical)
    marker = File.join(canonical, INCOMPLETE_NAME)
    raise Error, "incomplete workspace creation: #{marker}" if entry(marker)

    paths = {}
    REQUIRED.each do |name|
      child = File.join(canonical, name)
      directory(child)
      paths[name] = child
    end
    receipts = File.join(canonical, 'receipts')
    if entry(receipts)
      directory(receipts)
      paths['receipts'] = receipts
    else
      paths['receipts'] = nil
    end
    manifest = File.join(canonical, MANIFEST_NAME)
    managed = !entry(manifest).nil?
    if managed
      read_manifest(manifest)
      raise Error, 'managed layout has no receipts directory' unless paths['receipts']
    end
    current = directory(canonical)
    unless File.realpath(logical) == canonical &&
           [identity.dev, identity.ino] == [current.dev, current.ino]
      raise Error, 'workspace root changed during inspection'
    end

    {
      'format' => 'emberbsd-prebuild-sdk-inspection',
      'version' => 1,
      'scope' => 'workspace-layout',
      'logical_root' => logical,
      'canonical_root' => canonical,
      'structural_status' => managed ? 'managed' : 'adoptable-existing',
      'qualification' => 'not-assessed',
      'not_checked' => %w[ABI ELF compiler sysroot-cohort packages SDK-release],
      'manifest' => managed ? manifest : nil,
      'directories' => paths
    }
  rescue SystemCallError, IOError => e
    raise Error, e.message
  end

  def create(root, adopt: nil)
    logical = path(root)
    path(File.realpath(File.dirname(logical)))
    return adopt_layout(logical, adopt) if adopt

    # mkdir is the collision check. Never populate an existing destination.
    Dir.mkdir(logical)
    marker = File.join(logical, INCOMPLETE_NAME)
    File.open(marker, File::WRONLY | File::CREAT | File::EXCL, 0o644) do |file|
      file.write("workspace creation is incomplete\n")
    end
    DIRECTORIES.each { |name| Dir.mkdir(File.join(logical, name)) }
    manifest = File.join(logical, MANIFEST_NAME)
    File.open(manifest, File::WRONLY | File::CREAT | File::EXCL, 0o644) do |file|
      file.write(JSON.pretty_generate(MANIFEST) + "\n")
      file.flush
      file.fsync
    end
    # A failed create keeps its marker and partial contents for inspection.
    # Do not recursively remove paths that another process could have filled.
    File.unlink(marker)
    inspect_layout(logical)
  rescue SystemCallError, IOError => e
    raise Error, e.message
  end

  def adopt_layout(logical, existing)
    source = path(existing)
    directory(source)
    expected = inspect_layout(source).fetch('canonical_root')
    begin
      # symlink is atomic and does not replace a colliding destination.
      begin
        File.symlink(expected, logical)
      rescue NotImplementedError => e
        raise Error, "symbolic-link adoption is unavailable: #{e.message}"
      end
    rescue Errno::EEXIST
      current = entry(logical)
      unless current && current.symlink? && File.realpath(logical) == expected
        raise Error, "destination is not an alias to the requested workspace: #{logical}"
      end
    end
    result = inspect_layout(logical)
    raise Error, 'alias target changed during adoption' unless result['canonical_root'] == expected

    result
  rescue SystemCallError, IOError => e
    raise Error, e.message
  end
end
