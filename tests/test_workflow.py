from pathlib import Path
import unittest


WORKFLOW = Path(".github/workflows/refresh.yml").read_text(encoding="utf-8")


class RefreshWorkflowTests(unittest.TestCase):
    def test_cloudflare_publication_is_explicitly_opt_in(self):
        self.assertIn("if: vars.CLOUDFLARE_DUAL_PUBLISH == 'true'", WORKFLOW)
        self.assertIn(
            "CLOUDFLARE_ENABLED: ${{ vars.CLOUDFLARE_DUAL_PUBLISH == 'true' }}",
            WORKFLOW,
        )
        self.assertIn("python publication_gate.py", WORKFLOW)

    def test_catalog_guard_precedes_both_publishers(self):
        guard = WORKFLOW.index("- name: Protect against suspicious catalog drops")
        cloudflare = WORKFLOW.index("- name: Publish normalized snapshot to Cloudflare")
        vercel = WORKFLOW.index("- name: Deploy to Vercel")

        self.assertLess(guard, cloudflare)
        self.assertLess(guard, vercel)
        self.assertIn("allow_catalog_drop:", WORKFLOW)
        self.assertIn("steps.catalog_guard.outcome == 'success'", WORKFLOW)

    def test_live_site_comparison_and_force_deploy_remain(self):
        self.assertIn("Detect drift against the LIVE SITE", WORKFLOW)
        self.assertIn("inputs.force_deploy", WORKFLOW)
        self.assertIn("VERCEL_ORG_ID", WORKFLOW)
        self.assertIn("VERCEL_PROJECT_ID", WORKFLOW)

    def test_adzuna_secrets_are_scoped_to_scrape_step(self):
        scrape_step = WORKFLOW.index("- name: Scrape and verify postings")
        drift_step = WORKFLOW.index("- name: Detect drift against the LIVE SITE")
        scoped = WORKFLOW[scrape_step:drift_step]

        self.assertIn("ADZUNA_APP_ID: ${{ secrets.ADZUNA_APP_ID }}", scoped)
        self.assertIn("ADZUNA_APP_KEY: ${{ secrets.ADZUNA_APP_KEY }}", scoped)


if __name__ == "__main__":
    unittest.main()
