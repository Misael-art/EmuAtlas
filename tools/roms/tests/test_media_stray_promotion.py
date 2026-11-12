"""F09: strays are promoted only after full decode and an exact unique ROM basename."""
import json
import csv
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.roms import media_stray, media_session, media_optimizer

MAGICK = shutil.which("magick")


def png(path, color):
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([MAGICK, "-size", "4x4", f"xc:{color}", str(path)], check=True)


@unittest.skipUnless(MAGICK, "ImageMagick ausente")
class PromotionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name)
        (self.p / "frontend/RetroFE/Config").mkdir(parents=True)
        (self.p / "frontend/RetroFE/Config/platform-catalog.json").write_text(json.dumps({"systems": [{"id": "nes", "extensions": ["zip", "cue", "bin"]}]}))
        (self.p / "roms/nes").mkdir(parents=True)
        for name in ("Absent Game.zip", "Diff Game.zip", "Dup Game.zip"):
            (self.p / "roms/nes" / name).write_bytes(b"rom")
        (self.p / "roms/nes/a").mkdir(); (self.p / "roms/nes/b").mkdir()
        (self.p / "roms/nes/a/Twin.zip").write_bytes(b"1"); (self.p / "roms/nes/b/Twin.zip").write_bytes(b"2")
        stray, canon = self.p / "roms/media/nes/screenshot", self.p / "media/nes/screenshot"
        canon.mkdir(parents=True)
        png(stray / "Absent Game.png", "red")
        png(stray / "Diff Game.png", "blue"); png(canon / "Diff Game.png", "green")
        png(stray / "Dup Game.png", "white"); png(canon / "Dup Game.png", "black"); shutil.copy(stray / "Dup Game.png", canon / "Dup Game (Alt 2).png")
        png(stray / "Orphan.png", "red")
        png(stray / "Twin.png", "red")
        (stray / "Broken Game.png").write_bytes(b"\x89PNG\r\n\x1a\nbroken")
        (self.p / "roms/nes/Broken Game.zip").write_bytes(b"rom")
        self.api = {"PROJECT_ROOT": self.p, "OUTPUT_ROOT": self.p / "outputs", "media_session": media_session,
                    "media_optimizer": media_optimizer, "now": lambda: "now",
                    "operation_dir": lambda op: self.p / "outputs/ops" / op,
                    "atomic_json": lambda path, value: (path.parent.mkdir(parents=True, exist_ok=True), path.write_text(json.dumps(value))),
                    "append_event": lambda *a, **k: None, "rebuild_history_index": lambda: None,
                    "retrofe_processes_running": lambda: [], "esde_processes_running": lambda: []}

    def test_plan_apply_verify_rollback(self):
        result = media_stray.plan(self.api, promote=True)
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        by = {Path(i["relative_path"]).name: i for i in manifest["items"]}
        self.assertEqual(by["Absent Game.png"]["action"], "promote_to_canonical")
        self.assertEqual(by["Diff Game.png"]["destination_relative"], "media/nes/screenshot/Diff Game (Alt 2).png")
        self.assertEqual(by["Dup Game.png"]["action"], media_stray.QUARANTINE_ACTION)
        self.assertIn("sem ROM", by["Orphan.png"]["promotion_blocker"])
        self.assertIn("mais de uma", by["Twin.png"]["promotion_blocker"])
        self.assertIn("validação integral", by["Broken Game.png"]["promotion_blocker"])
        before_green = (self.p / "media/nes/screenshot/Diff Game.png").read_bytes()
        media_stray.apply(result["operation_id"], True, self.api)
        self.assertTrue((self.p / "media/nes/screenshot/Absent Game.png").is_file())
        self.assertEqual((self.p / "media/nes/screenshot/Diff Game.png").read_bytes(), before_green)
        self.assertTrue((self.p / "roms/media/nes/screenshot/Orphan.png").is_file())
        self.assertEqual(media_stray.verify(result["operation_id"], self.api)["status"], "verified")
        media_stray.rollback(result["operation_id"], True, self.api)
        self.assertTrue((self.p / "roms/media/nes/screenshot/Absent Game.png").is_file())
        self.assertFalse((self.p / "media/nes/screenshot/Absent Game.png").exists())
        self.assertFalse((self.p / "media/nes/screenshot/Diff Game (Alt 2).png").exists())

    def test_apply_refuses_occupied_destination(self):
        result = media_stray.plan(self.api, promote=True)
        png(self.p / "media/nes/screenshot/Absent Game.png", "yellow")
        with self.assertRaises(ValueError):
            media_stray.apply(result["operation_id"], True, self.api)
        self.assertTrue((self.p / "roms/media/nes/screenshot/Absent Game.png").is_file())

    def test_saves_and_artwork_do_not_become_additional_rom_candidates(self):
        (self.p / "roms/nes/Absent Game.sav").write_bytes(b"protected save")
        png(self.p / "roms/nes/art/Absent Game.png", "blue")
        result = media_stray.plan(self.api, promote=True)
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        item = next(x for x in manifest["items"] if x["relative_path"] == "nes/screenshot/Absent Game.png")
        self.assertEqual(item["action"], "promote_to_canonical")
        self.assertEqual((self.p / "roms/nes/Absent Game.sav").read_bytes(), b"protected save")

    def test_missing_category_requires_explicit_review_and_rolls_back_its_directories(self):
        source = self.p / "roms/media/nes/background/Absent Game.png"
        png(source, "red")
        def load(result):
            return json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        ordinary = load(media_stray.plan(self.api, promote=True))
        item = next(x for x in ordinary["items"] if x["project_relative"].endswith("background/Absent Game.png"))
        self.assertIn("criação de pasta exige revisão", item["promotion_blocker"])
        reviewed = media_stray.plan(self.api, promote=True, create_categories=["nes/background"])
        media_stray.apply(reviewed["operation_id"], True, self.api)
        self.assertTrue((self.p / "media/nes/background/Absent Game.png").is_file())
        self.assertFalse(source.exists())
        self.assertEqual(media_stray.verify(reviewed["operation_id"], self.api)["status"], "verified")
        media_stray.rollback(reviewed["operation_id"], True, self.api)
        self.assertTrue(source.is_file())
        self.assertFalse((self.p / "media/nes/background").exists())

    def test_reviewed_category_does_not_bypass_identity_or_symlink_guards(self):
        png(self.p / "roms/media/nes/background/Orphan.png", "red")
        result = media_stray.plan(self.api, promote=True, create_categories=["nes/background"])
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        item = next(x for x in manifest["items"] if x["project_relative"].endswith("background/Orphan.png"))
        self.assertIn("sem ROM", item["promotion_blocker"])
        self.assertFalse((self.p / "media/nes/background").exists())
        png(self.p / "roms/media/nes/background/Absent Game.png", "red")
        (self.p / "media/nes/background").symlink_to("screenshot", target_is_directory=True)
        result = media_stray.plan(self.api, promote=True, create_categories=["nes/background"])
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        item = next(x for x in manifest["items"] if x["project_relative"].endswith("background/Absent Game.png"))
        self.assertIn("symlink", item["promotion_blocker"])

    def test_invalid_reviewed_category_is_rejected_without_mutation(self):
        for category in ["../background", "nes/../background", "unknown/background", "nes/misc", "/nes/background"]:
            with self.subTest(category=category), self.assertRaises(ValueError):
                media_stray.plan(self.api, promote=True, create_categories=[category])

    def test_nested_disc_set_uses_valid_cue_without_counting_its_track_as_another_game(self):
        from tools.roms import romtool
        game_dir = self.p / "roms/nes/Disc Game"
        game_dir.mkdir()
        (game_dir / "Disc Game.bin").write_bytes(b"track fixture")
        (game_dir / "Disc Game.cue").write_text('FILE "Disc Game.bin" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\n')
        self.api["verify_cue"] = romtool.verify_cue
        source = self.p / "roms/media/nes/screenshot/Disc Game/Disc Game.png"
        png(source, "red")
        result = media_stray.plan(self.api, promote=True, nested_game_paths=True)
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        item = next(x for x in manifest["items"] if x["project_relative"] == source.relative_to(self.p).as_posix())
        self.assertEqual(item["action"], "promote_to_canonical")
        self.assertTrue(item["rom_association"].endswith("Disc Game.cue"))
        self.assertEqual(item["disc_set_structure"]["status"], "valid")
        (self.p / "roms/nes/Disc Game.zip").write_bytes(b"different game candidate")
        result = media_stray.plan(self.api, promote=True, nested_game_paths=True)
        manifest = json.loads((self.p / "outputs/ops" / result["operation_id"] / "manifest.json").read_text())
        item = next(x for x in manifest["items"] if x["project_relative"] == source.relative_to(self.p).as_posix())
        self.assertIn("mais de uma ROM", item["promotion_blocker"])

    def test_promoted_destination_updates_catalog_and_resolves_the_global_pending_row(self):
        from tools.roms import romtool
        output = self.p / "outputs"
        output.mkdir()
        self.api["write_csv_atomic"] = romtool.write_csv_atomic
        self.api["atomic_json"] = romtool.atomic_json
        fields = ["system_id", "source", "absolute_path", "current_absolute_path", "current_path_status", "current_sha256", "current_size_bytes", "current_operation_id"]
        catalog = output / "catalogo-arquivos.csv"
        romtool.write_csv_atomic(catalog, [], fields)
        pending = output / "pendencias-integridade-dependencias.csv"
        pending_fields = ["type", "source", "path", "status", "dependency_or_impact", "next_step", "case_sha256", "case_source_relative"]
        relative = "roms/media/nes/screenshot/Absent Game.png"
        romtool.write_csv_atomic(pending, [{"source": "roms-media-stray", "path": relative, "case_source_relative": relative}], pending_fields)
        state = output / "estado-final-verificacao.json"
        state.write_text('{"existing": true}')
        originals = {p: p.read_bytes() for p in [catalog, pending, state]}
        result = media_stray.plan(self.api, promote=True)
        media_stray.apply(result["operation_id"], True, self.api)
        with catalog.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertTrue(any(r["current_absolute_path"] == str(self.p / "media/nes/screenshot/Absent Game.png") for r in rows))
        with pending.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(next(r for r in rows if r["case_source_relative"] == relative)["status"], "resolvido: promoted")
        media_stray.rollback(result["operation_id"], True, self.api)
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)

    def test_nested_variant_is_flattened_only_with_rom_in_that_variant(self):
        (self.p / "roms/nes/translation-pt-br").mkdir()
        (self.p / "roms/nes/translation-pt-br/Jogo (Traduzido).zip").write_bytes(b"rom")
        png(self.p / "roms/media/nes/screenshot/translation-pt-br/Jogo (Traduzido).png", "red")
        png(self.p / "roms/media/nes/screenshot/hack/Absent Game.png", "purple")
        plain = media_stray.plan(self.api, promote=True)
        nested = media_stray.plan(self.api, promote=True, nested_variants=True)
        load = lambda r: {i.get("nested_relative_path", i["relative_path"]): i for i in json.loads(
            (self.p / "outputs/ops" / r["operation_id"] / "manifest.json").read_text())["items"]}
        self.assertEqual(load(plain)["nes/screenshot/translation-pt-br/Jogo (Traduzido).png"]["action"], "preserve_unidentified")
        rows = load(nested)
        self.assertEqual(rows["nes/screenshot/translation-pt-br/Jogo (Traduzido).png"]["destination_relative"],
                         "media/nes/screenshot/Jogo (Traduzido).png")
        self.assertIn("pasta de variante", rows["nes/screenshot/hack/Absent Game.png"]["promotion_blocker"])
        media_stray.apply(nested["operation_id"], True, self.api)
        self.assertTrue((self.p / "media/nes/screenshot/Jogo (Traduzido).png").is_file())
        self.assertTrue((self.p / "roms/media/nes/screenshot/hack/Absent Game.png").is_file())
        self.assertEqual(media_stray.verify(nested["operation_id"], self.api)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
