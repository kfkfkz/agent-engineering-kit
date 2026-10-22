"""Legacy ownership history is reproducible from the checkout, not local refs."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer.legacy import generate_legacy_hashes


class CheckoutHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'source'
        self.repo.mkdir()
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
                    'GIT_CONFIG_GLOBAL': os.devnull}
        self.git('init', '-b', 'main')
        self.template = self.repo / 'templates/claude-md-section.md'
        self.template.parent.mkdir()
        self.template.write_bytes(b'Published template\n')
        self.commit()
        self.published = self.git('rev-parse', '--short', 'HEAD').stdout.strip()
        self.baseline = generate_legacy_hashes(self.repo)
        self.assertIn(self.published, self.baseline['kit_versions'])

    def git(self, *args, repo=None):
        return subprocess.run(['git', '-C', str(repo or self.repo),
            '-c', 'user.name=History Fixture', '-c', 'user.email=fixture@example.invalid',
            *args], env=self.env, capture_output=True, text=True, check=True)

    def commit(self):
        self.git('add', '--', 'templates/claude-md-section.md')
        self.git('commit', '-m', 'fixture template revision')

    def test_private_stash_does_not_grant_legacy_ownership(self):
        private = b'Private unsubmitted constitution\n'
        self.template.write_bytes(private)
        self.git('stash', 'push', '-m', 'local private work')
        result = generate_legacy_hashes(self.repo)
        self.assertEqual(result, self.baseline)
        self.assertNotIn('canonical_sha256:' + hashlib.sha256(private).hexdigest(),
            [value for row in result['kit_versions'].values() for value in row.values()])

    def test_unrelated_branch_does_not_change_checkout_history(self):
        self.git('switch', '-c', 'local-unpublished-work')
        self.template.write_bytes(b'Unpublished branch template\n')
        self.commit()
        self.git('switch', 'main')
        self.assertEqual(generate_legacy_hashes(self.repo), self.baseline)

    def test_clean_single_branch_clone_has_the_same_legacy_history(self):
        self.template.write_bytes(b'Private stashed revision\n')
        self.git('stash', 'push', '-m', 'private')
        clone = Path(self.temp.name) / 'clean'
        self.git('clone', '--no-local', '--single-branch', '--branch', 'main',
                 str(self.repo), str(clone))
        self.assertEqual(generate_legacy_hashes(self.repo), generate_legacy_hashes(clone))

    def test_committed_ancestor_versions_remain_available(self):
        self.template.write_bytes(b'Next published template\n')
        self.commit()
        current = self.git('rev-parse', '--short', 'HEAD').stdout.strip()
        result = generate_legacy_hashes(self.repo)['kit_versions']
        for ref in (self.published, current):
            self.assertIn(ref, result)
            self.assertIn('claude-md-block', result[ref])
            self.assertIn('claude-md-block', result[ref + ':legacy-section'])

    def test_merged_parent_history_is_not_hidden_by_path_simplification(self):
        self.git('switch', '-c', 'maintenance')
        self.template.write_bytes(b'Published maintenance template\n')
        self.commit()
        maintenance = self.git('rev-parse', '--short', 'HEAD').stdout.strip()
        self.git('tag', 'published-maintenance-fixture')
        self.git('switch', 'main')
        self.git('merge', '--no-ff', '-s', 'ours', 'maintenance', '-m', 'retain current template')
        self.git('merge-base', '--is-ancestor', maintenance, 'HEAD')
        self.git('cat-file', '-e', maintenance + ':templates/claude-md-section.md')
        result = generate_legacy_hashes(self.repo)['kit_versions']
        self.assertIn(maintenance, result)
        self.assertIn('claude-md-block', result[maintenance])
        self.assertIn('claude-md-block', result[maintenance + ':legacy-section'])


if __name__ == '__main__':
    unittest.main()
