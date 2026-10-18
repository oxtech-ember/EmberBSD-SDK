#!/usr/bin/env ruby
# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
# Origin: EmberBSD; AI-assisted native OS build orchestration.

require 'optparse'
require_relative '../lib/sysroot_builder'

options = {}
parser = OptionParser.new do |p|
  p.banner = 'Usage: build-sysroot.rb --sdk ROOT --os SOURCE --ports SOURCE --compiler PREFIX --runtime-work WORK --gcc-archive ARCHIVE --pkgsrc EXPORT --target aarch64|amd64 --jobs N [--plan] [--destination DIR] [--objects DIR] [--tools DIR]'
  %w[sdk os ports compiler runtime-work gcc-archive pkgsrc target jobs destination objects tools].each do |name|
    p.on("--#{name} VALUE") do |value|
      raise OptionParser::InvalidArgument, "duplicate --#{name}" if options.key?(name)
      options[name] = value
    end
  end
  p.on('--plan') { options['plan'] = true }
  p.on('--help') { puts p; exit 0 }
end
begin
  parser.parse!
  raise OptionParser::InvalidArgument, 'unexpected positional arguments' unless ARGV.empty?
  %w[sdk os ports compiler runtime-work gcc-archive pkgsrc target jobs].each do |name|
    raise OptionParser::MissingArgument, "--#{name}" unless options.key?(name)
  end
  puts JSON.pretty_generate(PrebuildSDK::SysrootBuilder.new(options).run)
rescue OptionParser::ParseError => e
  warn "#{e.message}\n#{parser}"
  exit 2
rescue Interrupt => e
  warn "sysroot build interrupted: #{e.message}"
  exit 130
rescue PrebuildSDK::Error, SystemCallError, IOError => e
  warn "sysroot build: #{e.message}"
  exit 1
end
