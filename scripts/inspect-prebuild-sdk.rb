#!/usr/bin/env ruby
# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD; AI-assisted read-only workspace inspection CLI.
require_relative '../lib/prebuild_sdk'

unless ARGV.length == 1 && !ARGV[0].start_with?('-')
  warn 'Usage: inspect-prebuild-sdk.rb ROOT'
  exit 2
end
begin
  puts JSON.pretty_generate(PrebuildSDK.inspect_layout(ARGV[0]))
rescue PrebuildSDK::Error => e
  warn "error: #{e.message}"
  exit 1
end
