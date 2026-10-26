# SPDX-License-Identifier: BSD-2-Clause
# Copyright (c) 2026 EmberBSD contributors.
"""Run the same component gates against the retained version 1 source contract."""
import test_bootstrap_compat_listing as current


class RetainedListingTests(current.ListingComponentTests):
    source_variable = 'EMBERBSD_LISTING_V1_SOURCE'
    member_count = 35
    manifest_version = 1
