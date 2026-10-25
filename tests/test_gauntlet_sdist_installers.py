"""Exercise the real offline sdist backend, never native installation."""
from pathlib import Path, PurePosixPath
import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ('install_dev17_profile.sh', 'install_runtime_payload.py',
              'install_profile_transaction.py')


@unittest.skipUnless(importlib.util.find_spec('setuptools'), 'Offline setuptools backend is unavailable')
class TestSourceDistributionInstallerDelivery(unittest.TestCase):
    def test_real_sdist_delivers_exact_documented_installer_and_helpers(self):
        with tempfile.TemporaryDirectory(prefix='nerve-sdist-') as temporary:
            work = Path(temporary)
            source, out = work / 'source', work / 'dist'
            api = runpy.run_path(str(ROOT / 'scripts/install_runtime_payload.py'))
            api['install'](ROOT, source)
            (source / 'scripts').mkdir()
            for name in INSTALLERS:
                shutil.copy2(ROOT / 'scripts' / name, source / 'scripts' / name)
            if (ROOT / 'MANIFEST.in').exists():
                shutil.copy2(ROOT / 'MANIFEST.in', source / 'MANIFEST.in')
            fixture = 'tests/fixtures/policy-members-b486692.json'
            (source / fixture).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / fixture, source / fixture)
            for name in ('home', 'profile', 'tmp', 'dist'):
                (work / name).mkdir()
            env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
            env.update(HOME=str(work / 'home'), HERMES_HOME=str(work / 'profile'),
                       TMPDIR=str(work / 'tmp'), PYTHONDONTWRITEBYTECODE='1',
                       OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
            program = (
                'import sys,json,importlib.metadata; from setuptools import build_meta; '
                'print("BACKEND_PROVENANCE="+json.dumps({"module":build_meta.__file__, '
                '"version":importlib.metadata.version("setuptools"),"python":sys.executable})); '
                'print("BUILT_SDIST="+build_meta.build_sdist(sys.argv[1]))'
            )
            started = time.monotonic()
            result = subprocess.run([sys.executable, '-B', '-c', program, str(out)],
                                    cwd=source, env=env, capture_output=True, text=True, timeout=15)
            elapsed = time.monotonic() - started
            archives = list(out.glob('*.tar.gz'))
            evidence = os.environ.get('NERVE_DISTRIBUTION_EVIDENCE_DIR')
            if evidence:
                target = Path(evidence)
                target.mkdir(parents=True, exist_ok=False)
                (target / 'build.log').write_text(result.stdout + result.stderr)
                (target / 'build-result.json').write_text(json.dumps({
                    'returncode': result.returncode, 'elapsed_seconds': elapsed,
                    'command': [sys.executable, '-B', '-c', program, str(out)],
                    'snapshot_kind': 'declared-runtime-plus-three-installers-and-optional-manifest',
                    'native_installation': False,
                }, indent=2) + '\n')
                for archive in archives:
                    shutil.copy2(archive, target / archive.name)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0]) as archive:
                files = {}
                roots = set()
                for member in archive.getmembers():
                    path = PurePosixPath(member.name)
                    self.assertFalse(path.is_absolute() or '..' in path.parts, member.name)
                    self.assertTrue(member.isfile() or member.isdir(), member.name)
                    roots.add(path.parts[0])
                    if member.isfile():
                        payload = archive.extractfile(member)
                        if payload is None:
                            raise AssertionError('Regular archive member is unreadable: ' + member.name)
                        files[str(PurePosixPath(*path.parts[1:]))] = payload.read()
            self.assertEqual(len(roots), 1)
            required = ['scripts/' + name for name in INSTALLERS] + [fixture]
            self.assertEqual([name for name in required if name not in files], [],
                             'Source archive omits its documented installer or required helpers')
            for name in required:
                self.assertEqual(files[name], (ROOT / name).read_bytes(), name)
            for path in api['runtime_files'](ROOT):
                name = str(path.relative_to(ROOT))
                self.assertIn(name, files)
                self.assertEqual(files[name], path.read_bytes(), name)
            for name in files:
                self.assertNotIn('__pycache__', PurePosixPath(name).parts)
                self.assertFalse(name.endswith(('.pyc', '.pyo')), name)


if __name__ == '__main__':
    unittest.main()
