#!/usr/bin/env ruby
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted workspace layout CLI.
require_relative '../lib/prebuild_sdk'

unless (ARGV.length == 1 || (ARGV.length == 3 && ARGV[1] == '--adopt')) && !ARGV[0].start_with?('-')
  warn 'Usage: create-prebuild-sdk.rb ROOT [--adopt EXISTING]'
  exit 2
end
begin
  result = PrebuildSDK.create(ARGV[0], adopt: ARGV.length == 3 ? ARGV[2] : nil)
  puts JSON.pretty_generate(result)
rescue PrebuildSDK::Error => e
  warn "error: #{e.message}"
  exit 1
end
