import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup


class ReviewedPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = setup.Installer(self.root)
        self.engine.log = io.StringIO()
        self.source = self.root / 'work/installation/builds/fixture/source'
        self.source.mkdir(parents=True)
        self.path = self.source / 'file.cpp'
        self.original = b'first\nsecond\n'
        self.path.write_bytes(self.original)
        self.patches = [
            {'file': 'file.cpp', 'before': 'first', 'after': 'first extended'},
            {'file': 'file.cpp', 'before': 'second', 'after': 'second extended'},
        ]

    def git(self, *arguments):
        return subprocess.check_output(['git', '-C', str(self.source), *arguments],
                                       stderr=subprocess.STDOUT, text=True).strip()

    def test_patched_git_source_resumes_and_personal_changes_are_preserved(self):
        self.git('init')
        self.git('add', 'file.cpp')
        self.git('-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-m', 'own fixture')
        recipe = {'repository': 'https://github.com/fixture/fixture.git',
                  'commit': self.git('rev-parse', 'HEAD'), 'patches': self.patches}
        self.path.write_bytes(b'first extended\nsecond extended\n')
        with patch.object(self.engine, 'run', side_effect=AssertionError('no fetch or checkout over patched source')):
            self.assertEqual(self.engine.source_tree('fixture', recipe)[1], self.source)
            self.path.write_bytes(self.path.read_bytes() + b'personal change\n')
            with self.assertRaisesRegex(ValueError, 'preservada'):
                self.engine.source_tree('fixture', recipe)
        self.assertIn(b'personal change', self.path.read_bytes())

    def test_archive_source_accepts_multiple_reviewed_patches_on_one_file(self):
        recipe = {'source': {'sha256': 'a' * 64}, 'patches': self.patches}
        marker = self.source.parent / 'source-origin.json'
        marker.write_text(json.dumps({'sha256': 'a' * 64,
            'files': {'file.cpp': hashlib.sha256(self.original).hexdigest()}}))
        self.path.write_bytes(b'first extended\nsecond extended\n')
        with patch.object(self.engine, 'acquire', return_value=self.root / 'own archive fixture'):
            self.assertEqual(self.engine.source_tree('fixture', recipe)[1], self.source)
            self.path.write_bytes(self.path.read_bytes() + b'personal change\n')
            with self.assertRaisesRegex(ValueError, 'mudou'):
                self.engine.source_tree('fixture', recipe)

    def test_retry_does_not_repeat_a_patch_containing_its_original_text(self):
        output = self.source.parent / 'binary'
        output.write_bytes(b'own executable fixture')
        recipe = {'target': 'frontend/fixture', 'output': str(output),
                  'commands': [], 'patches': self.patches}
        self.path.write_bytes(b'first extended\nsecond extended\n')
        with patch.object(self.engine, 'source_tree', return_value=(self.source.parent, self.source)), \
             patch.object(self.engine, 'check_elf'):
            self.engine.build('fixture', recipe)
        self.assertEqual(self.path.read_bytes(), b'first extended\nsecond extended\n')


class WindowCloseWatcherTests(unittest.TestCase):
    def test_native_close_survives_event_drain_and_rearm_and_unhooks_on_destruction(self):
        compiler = shutil.which('c++')
        if not compiler:
            self.skipTest('C++ compiler unavailable for the isolated event callback regression')
        recipe = json.loads(setup.RECIPE_PATH.read_text())['components']['retrofe']
        helper = next(row['after'].split('RetroFE::RetroFE')[0]
                      for row in recipe['patches'] if row['before'].startswith('RetroFE::RetroFE'))
        source = '''#include <atomic>
#include <cassert>
#define SDLCALL
enum { SDL_QUIT = 1, SDL_WINDOWEVENT = 2, SDL_WINDOWEVENT_CLOSE = 3 };
struct SDL_Event { int type; struct { int event; } window; };
using Filter = int (*)(void*, SDL_Event*);
Filter callback = nullptr;
void *context = nullptr;
void SDL_DelEventWatch(Filter f, void *p) {
    if (callback == f && context == p) { callback = nullptr; context = nullptr; }
}
void SDL_AddEventWatch(Filter f, void *p) { assert(!callback); callback = f; context = p; }
''' + helper + '''
int main() {
    {
        WindowCloseWatcher watch;
        SDL_Event event{SDL_WINDOWEVENT, {SDL_WINDOWEVENT_CLOSE}};
        callback(context, &event);
        watch.rearm();
        assert(watch.consume());
        assert(!watch.consume());
        event.type = 99;
        callback(context, &event);
        assert(!watch.consume());
        event.type = SDL_QUIT;
        callback(context, &event);
        assert(watch.consume());
    }
    assert(!callback);
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            cpp = folder / 'close.cpp'
            binary = folder / 'close'
            cpp.write_text(source)
            subprocess.run([compiler, '-std=c++11', '-O0', str(cpp), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
