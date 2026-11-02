"""의존성 격리 테스트 — 별도 프로세스에서 anthropic / python-dotenv 를 차단하고 확인한다.

차단은 sys.modules[name] = None 으로 하므로 패키지가 설치돼 있어도, 없어도 같은 결과다.
월간(보호) 경로 collect.py / main.py 는 anthropic 이 필요하며, 이는 의도된 제약으로 별도 검증한다.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRELUDE = """
import sys
for _m in {blocked!r}:
    sys.modules[_m] = None
sys.path.insert(0, {root!r})
"""

FREE_PATH = ("dates", "content_quality", "backends", "backends.anthropic_backend", "collect_insight",
             "collect_lifestyle", "run_insight", "run_lifestyle")


def run(code, blocked, env_extra=None, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", PRELUDE.format(blocked=tuple(blocked), root=str(ROOT)) + code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(ROOT), timeout=120,
    )


BLOCK_SETS = (("anthropic",), ("anthropic", "dotenv"))


class FreePathWithoutOptionalPackagesTests(unittest.TestCase):
    def test_free_path_modules_import_without_sdk_or_dotenv(self):
        code = "import " + ", ".join(FREE_PATH) + "\nassert 'collect' not in sys.modules\nprint('IMPORT_OK')"
        for blocked in BLOCK_SETS:
            with self.subTest(blocked=blocked):
                r = run(code, blocked)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("IMPORT_OK", r.stdout)

    def test_adapter_import_does_not_import_sdk(self):
        r = run("import backends.anthropic_backend\nprint('anthropic' in sys.modules and sys.modules['anthropic'] is not None)", ())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_unset_backend_is_skipped_exit_0_even_without_packages(self):
        code = "import run_insight, run_lifestyle\nprint(run_insight.main()); print(run_lifestyle.main())"
        for blocked in BLOCK_SETS:
            with self.subTest(blocked=blocked):
                r = run(code, blocked, drop=("AI_BACKEND",))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.count("STATUS=SKIPPED"), 2)
                self.assertIn("DATA_CHANGED=false", r.stdout)

    def test_anthropic_backend_without_sdk_fails_clearly_exit_1(self):
        code = "import run_insight\nraise SystemExit(run_insight.main())"
        for blocked in BLOCK_SETS:
            with self.subTest(blocked=blocked):
                r = run(code, blocked, env_extra={"AI_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "dummy"})
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("STATUS=ERROR", r.stdout)
                self.assertIn("SDK is not installed", r.stdout)
                self.assertNotIn("dummy", r.stdout + r.stderr)

    def test_monthly_protected_path_still_requires_anthropic(self):
        # 의도된 제약: collect.py(월간, 보호 파일)는 최상단에서 anthropic 을 임포트한다.
        r = run("import collect", ("anthropic",))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("anthropic", r.stderr)


if __name__ == "__main__":
    unittest.main()
