import unittest

from publication_gate import publication_errors


class PublicationGateTests(unittest.TestCase):
    def test_disabled_cloudflare_skip_preserves_vercel_only_success(self):
        errors = publication_errors(
            catalog_guard_outcome="success",
            cloudflare_enabled=False,
            cloudflare_outcome="skipped",
            needs_vercel=True,
            vercel_outcome="success",
        )

        self.assertEqual(errors, [])

    def test_enabled_cloudflare_failure_blocks_commit(self):
        errors = publication_errors(
            catalog_guard_outcome="success",
            cloudflare_enabled=True,
            cloudflare_outcome="failure",
            needs_vercel=True,
            vercel_outcome="success",
        )

        self.assertEqual(errors, ["Cloudflare snapshot publication failed"])

    def test_catalog_guard_failure_blocks_all_publication(self):
        errors = publication_errors(
            catalog_guard_outcome="failure",
            cloudflare_enabled=False,
            cloudflare_outcome="skipped",
            needs_vercel=True,
            vercel_outcome="skipped",
        )

        self.assertEqual(errors, ["Catalog drop protection blocked publication"])

    def test_required_vercel_failure_blocks_commit(self):
        errors = publication_errors(
            catalog_guard_outcome="success",
            cloudflare_enabled=False,
            cloudflare_outcome="skipped",
            needs_vercel=True,
            vercel_outcome="failure",
        )

        self.assertEqual(errors, ["Vercel production deployment failed"])

    def test_skipped_vercel_is_allowed_when_live_site_is_current(self):
        errors = publication_errors(
            catalog_guard_outcome="success",
            cloudflare_enabled=False,
            cloudflare_outcome="skipped",
            needs_vercel=False,
            vercel_outcome="skipped",
        )

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
