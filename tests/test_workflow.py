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

    def test_adzuna_secrets_are_scoped_to_source_steps(self):
        scrape_step = WORKFLOW.index("- name: Scrape and verify postings")
        drift_step = WORKFLOW.index("- name: Detect drift against the LIVE SITE")
        scoped = WORKFLOW[scrape_step:drift_step]

        self.assertIn("ADZUNA_APP_ID: ${{ secrets.ADZUNA_APP_ID }}", scoped)
        self.assertIn("ADZUNA_APP_KEY: ${{ secrets.ADZUNA_APP_KEY }}", scoped)
        self.assertIn(
            "ADZUNA_BASELINE_PATH: ${{ runner.temp }}/checked-in-live_jobs.json",
            scoped,
        )
        self.assertIn(
            "ADZUNA_FORCE_REFRESH: ${{ inputs.force_adzuna_refresh }}",
            scoped,
        )
        self.assertIn("- name: Run sanitized Adzuna coverage check", WORKFLOW)
        self.assertIn("python adzuna_dry_run.py", WORKFLOW)

    def test_adzuna_dry_run_cannot_publish_or_commit(self):
        self.assertIn("adzuna_dry_run:", WORKFLOW)
        self.assertIn("if: inputs.adzuna_dry_run == true", WORKFLOW)
        self.assertIn("if: inputs.adzuna_dry_run != true", WORKFLOW)
        self.assertIn("actions/upload-artifact@v4", WORKFLOW)
        self.assertIn("force_adzuna_refresh:", WORKFLOW)
        self.assertIn('ADZUNA_FORCE_REFRESH: "true"', WORKFLOW)

    def test_public_boards_use_captured_baseline_without_changing_schedule(self):
        self.assertIn(
            "SOURCE_BASELINE_PATH: ${{ runner.temp }}/checked-in-live_jobs.json",
            WORKFLOW,
        )
        self.assertIn('- cron: "0 * * * *"', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
