import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.roms import variant_organization, media_session


PROJECT = Path(__file__).resolve().parents[3]


class AssociatedMediaValidationTests(unittest.TestCase):
    def test_full_validation_is_bound_to_the_current_bytes(self):
        (PROJECT / "outputs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT / "outputs") as raw:
            path = Path(raw) / "Game.mp4"
            path.write_bytes(b"fixture")
            api = {"sha256_file": lambda p: hashlib.sha256(p.read_bytes()).hexdigest()}
            with patch.object(media_session, "_validate", return_value={"streams": [{"codec_type": "video"}]}) as validate:
                result = variant_organization._validate_associated_media(path, api)
            self.assertEqual(result["validation"], "media_full_decode_v1")
            self.assertEqual(result["sha256"], api["sha256_file"](path))
            validate.assert_called_once()
            with patch.object(media_session, "_validate", side_effect=ValueError("decode failed")):
                with self.assertRaisesRegex(ValueError, "decode failed"):
                    variant_organization._validate_associated_media(path, api)

            def changed(*args):
                path.write_bytes(b"changed")
                return {}

            with patch.object(media_session, "_validate", side_effect=changed):
                with self.assertRaisesRegex(ValueError, "mudou"):
                    variant_organization._validate_associated_media(path, api)


class VariantOrganizationTransactionTests(unittest.TestCase):
    def test_pending_review_origin_is_read_from_mods_and_placed_under_roms(self):
        relative = variant_organization._safe_relative_path(
            "mods/pending/famicom/Game.nes", "famicom", review_origin=True)
        self.assertEqual(variant_organization._placement_parent(relative, "famicom"), Path("roms/famicom"))
        with self.assertRaises(ValueError):
            variant_organization._safe_relative_path("mods/pending/famicom/Game.nes", "famicom")
    def setUp(self):
        (PROJECT / "outputs").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="variant-organization-test-", dir=PROJECT / "outputs")
        self.root = Path(self.temp.name)
        self.ops = self.root / "outputs/operations"
        self.ops.mkdir(parents=True)
        self.rom_root = self.root / "roms/famicom"
        self.rom_root.mkdir(parents=True)
        retrofe = self.root / "frontend/RetroFE"
        (retrofe / "Config/profiles").mkdir(parents=True)
        (retrofe / "Config/platform-catalog.json").write_text(json.dumps({
            "systems": [{"id": "famicom", "kind": "platform", "rom_subdir": "famicom",
                         "extensions": ["7z", "fds", "nes", "unf", "unif", "zip"]}]
        }), encoding="utf-8")
        (retrofe / "Config/profiles/host-linux.json").write_text(json.dumps({
            "collections": {"Nintendo Family Computer": {
                "rom_subdir": "famicom", "extensions": "zip,7z,nes"}}
        }), encoding="utf-8")
        (retrofe / "collections/Main").mkdir(parents=True)
        (retrofe / "collections/Main/menu.txt").write_text("Nintendo Family Computer\n", encoding="utf-8")
        (retrofe / "settings.conf").write_text(f"baseItemPath = {self.root / 'roms'}\n", encoding="utf-8")
        self.esde = self.root / "ES-DE"
        (self.esde / "gamelists/famicom").mkdir(parents=True)
        self.gamelist = self.esde / "gamelists/famicom/gamelist.xml"
        self.before_xml = b'<?xml version="1.0"?>\r\n<gameList>\r\n  <game><path>./Demo (Unl).zip</path></game>\r\n</gameList>\r\n'
        self.gamelist.write_bytes(self.before_xml)
        self.source = self.rom_root / "Demo (Unl).zip"
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("Demo (Unl).nes", b"NES fixture payload")
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.payload_sha = hashlib.sha256(b"NES fixture payload").hexdigest()
        self._write_operation("inventory-fixture", {
            "kind": "rom_variant_inventory", "status": "completed_with_blockers", "hash_requested": True,
            "items": [{"system_id": "famicom", "path": "roms/famicom/Demo (Unl).zip",
                       "filename": self.source.name, "artifact_role": "catalog_supported_content_candidate",
                       "size_bytes": self.source.stat().st_size, "source_file_sha256": self.source_sha,
                       "hash_status": "hashed", "extension_supported": True,
                       "name_evidence": {"kind_candidates": [{"value": "unlicensed"}]}}]
        })
        self._write_operation("dat-fixture", {
            "kind": "dat_match", "status": "completed", "evidence": {"system_id": "famicom"},
            "items": [{"system_id": "famicom", "source_path": "roms/famicom/Demo (Unl).zip",
                       "status": "full_dat_match", "matching_dat_records": 1,
                       "archive_member_count": 1, "non_rom_member_count": 0,
                       "payload_member": "Demo (Unl).nes", "suggested_container_name": "Demo (Unl).zip",
                       "dat_rom_name": "Demo (Unl).nes", "dat_sha256": "a" * 64,
                       "payload_size_bytes": 19, "payload_crc32": "00000000",
                       "payload_md5": "b" * 32, "payload_sha1": "c" * 40,
                       "payload_sha256": self.payload_sha}]
        })
        self._write_operation("verify-fixture", {
            "kind": "verify", "status": "completed", "items": [{
                "path": str(self.source), "status": "ok", "sha256": self.source_sha,
                "member_count": 1, "encrypted_member_count": 0}]
        })
        self._write_operation("hierarchy-fixture", {
            "kind": "retrofe_rom_hierarchy", "status": "verified", "system_id": "famicom"}
        )
        self.processes = {"retrofe": [], "esde": []}

        def operation_dir(operation_id):
            return self.ops / operation_id

        def atomic_json(path, value):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        def append_event(operation_id, event, **values):
            path = operation_dir(operation_id) / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, **values}) + "\n")

        self.api = {
            "PROJECT_ROOT": self.root, "ESDE_HOME": self.esde,
            "variant_policy_path": PROJECT / "tools/roms/config/variant-policy.json",
            "operation_dir": operation_dir, "atomic_json": atomic_json,
            "append_event": append_event, "rebuild_history_index": lambda: None,
            "now": lambda: "2026-10-26T14:00:00-03:00",
            "sha256_file": lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "frontend_processes_running": lambda: self.processes,
        }

    def _write_operation(self, operation_id, manifest):
        directory = self.ops / operation_id
        directory.mkdir(parents=True)
        manifest["operation_id"] = operation_id
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _plan(self):
        return variant_organization.plan("famicom", "inventory-fixture", "dat-fixture",
                                         "verify-fixture", "hierarchy-fixture", self.api)

    def test_no_eligible_names_are_recorded_without_touching_roms(self):
        before=self.source.read_bytes()
        result=variant_organization.plan_dat_name_alignment('famicom','inventory-fixture','dat-fixture','verify-fixture',self.api)
        self.assertEqual(result['status'],'no_eligible_candidates')
        self.assertEqual(self.source.read_bytes(),before)

    def test_plan_apply_verify_rollback_updates_gamelist_atomically(self):
        retrofe_collection = self.root / "frontend/RetroFE/collections/Nintendo Family Computer"
        retrofe_collection.mkdir(parents=True)
        (retrofe_collection / "settings.conf").write_text(
            "list.path = %BASE_ITEM_PATH%/famicom\n", encoding="utf-8")
        retrofe_gamelist = retrofe_collection / "gamelist.xml"
        retrofe_gamelist.write_bytes(b"<gameList><game><path>./roms/Demo (Unl).zip</path></game></gameList>\n")
        local_gamelist = self.rom_root / "gamelist.xml"
        local_gamelist.write_bytes(b"<gameList><game><path>./Demo (Unl).zip</path></game></gameList>\n")

        result = self._plan()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["planned_files"], 1)
        self.assertEqual(result["reference_files"], 3)
        self.assertEqual(len(manifest["excluded_frontend_references"]), 0)
        self.assertEqual(self.source.read_bytes()[:2], b"PK")
        proposed_files = [Path(row["proposed_file"]).read_bytes()
                          for row in manifest["frontend_reference_updates"]]
        self.assertTrue(all(b"unlicensed/Demo (Unl).zip" in data for data in proposed_files))
        applied = variant_organization.apply(result["operation_id"], True, self.api)
        destination = self.rom_root / "unlicensed/Demo (Unl).zip"
        self.assertEqual(applied["status"], "completed_verified")
        self.assertTrue(destination.is_file())
        self.assertFalse(self.source.exists())
        self.assertIn(b"unlicensed/Demo (Unl).zip", retrofe_gamelist.read_bytes())
        self.assertIn(b"unlicensed/Demo (Unl).zip", local_gamelist.read_bytes())
        self.assertEqual(variant_organization.verify(result["operation_id"], self.api)["status"], "verified")
        rolled_back = variant_organization.rollback(result["operation_id"], True, self.api)
        self.assertEqual(rolled_back["status"], "rolled_back_verified")
        self.assertEqual(self.source.read_bytes()[:2], b"PK")
        self.assertEqual(self.gamelist.read_bytes(), self.before_xml)
        self.assertIn(b"./roms/Demo (Unl).zip", retrofe_gamelist.read_bytes())
        self.assertIn(b"./Demo (Unl).zip", local_gamelist.read_bytes())
        self.assertFalse((self.rom_root / "unlicensed").exists())

    def test_references_owned_by_a_different_platform_are_preserved_and_recorded(self):
        collection = self.root / "frontend/RetroFE/collections/Nintendo Entertainment System select"
        collection.mkdir(parents=True)
        (collection / "settings.conf").write_text(
            "list.path = %BASE_ITEM_PATH%/nes\n", encoding="utf-8")
        gamelist = collection / "gamelist.xml"
        original = b"<gameList><game><path>./roms/Demo (Unl).zip</path></game></gameList>\n"
        gamelist.write_bytes(original)

        result = self._plan()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["planned_files"], 1)
        self.assertEqual(result["reference_files"], 1)
        self.assertEqual(gamelist.read_bytes(), original)
        self.assertEqual(manifest["excluded_frontend_references"], [{
            "path": str(gamelist), "filename": "Demo (Unl).zip",
            "reason": "gamelist pertence a outra plataforma; referência preservada",
            "owner": str((self.root / "roms/nes").resolve()),
        }])

    def test_ambiguous_retrofe_owner_blocks_movement(self):
        collection = self.root / "frontend/RetroFE/collections/Unclassified"
        collection.mkdir(parents=True)
        gamelist = collection / "gamelist.xml"
        gamelist.write_bytes(b"<gameList><game><path>./roms/Demo (Unl).zip</path></game></gameList>\n")

        result = self._plan()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["planned_files"], 0)
        self.assertTrue(self.source.is_file())
        self.assertTrue(any(item["reason"] == "proprietário RetroFE não resolvido"
                            for item in manifest["blockers"]))

    def test_apply_waits_for_frontends_to_close(self):
        result = self._plan()
        self.processes = {"retrofe": [{"pid": 42}]}
        with self.assertRaisesRegex(RuntimeError, "Feche RetroFE"):
            variant_organization.apply(result["operation_id"], True, self.api)
        self.assertTrue(self.source.is_file())
        self.assertEqual(self.gamelist.read_bytes(), self.before_xml)

    def test_no_eligible_candidates_are_recorded_with_blockers(self):
        dat_path = self.ops / "dat-fixture/manifest.json"
        dat_manifest = json.loads(dat_path.read_text(encoding="utf-8"))
        dat_manifest["items"][0]["status"] = "no_match_retain"
        dat_manifest["items"][0]["matching_dat_records"] = 0
        dat_path.write_text(json.dumps(dat_manifest), encoding="utf-8")
        result = self._plan()
        self.assertEqual(result["status"], "no_eligible_candidates")
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertFalse(manifest["apply_available"])
        self.assertEqual(manifest["blockers"][0]["reason"], "no_unique_full_dat_match")
        self.assertTrue(self.source.is_file())

    def test_cancel_marks_superseded_plan_without_touching_rom_or_proposal(self):
        result = self._plan()
        manifest_path = self.ops / result["operation_id"] / "manifest.json"
        manifest_before = json.loads(manifest_path.read_text())
        proposal = Path(manifest_before["frontend_reference_updates"][0]["proposed_file"])
        cancelled = variant_organization.cancel_prepared(
            result["operation_id"], "substituído por plano que referencia hierarquia abrangente", self.api)
        self.assertEqual(cancelled["status"], "cancelled_superseded")
        self.assertTrue(self.source.is_file())
        self.assertTrue(proposal.is_file())
        current = json.loads(manifest_path.read_text())
        self.assertEqual(current["cancellation_reason"], "substituído por plano que referencia hierarquia abrangente")

    def test_apply_rejects_stale_gamelist_without_moving_rom(self):
        result = self._plan()
        self.gamelist.write_bytes(self.before_xml + b"<!-- user edit -->\r\n")
        with self.assertRaisesRegex(ValueError, "Gamelist mudou"):
            variant_organization.apply(result["operation_id"], True, self.api)
        self.assertTrue(self.source.is_file())

    def test_nointro_name_plan_aligns_rom_story_and_gamelist(self):
        source = self.rom_root / "Name Before.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("Name Before.nes", b"verified payload")
        payload = b"Hist\xf3ria completa do jogo.\r\n"
        media_dir = self.root / "media/famicom/story"
        media_dir.mkdir(parents=True)
        media = media_dir / "Name Before.txt"
        media.write_bytes(payload)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        payload_sha = hashlib.sha256(b"verified payload").hexdigest()
        before_xml = self.gamelist.read_bytes()
        self.gamelist.write_bytes(
            before_xml.replace(b"</gameList>",
                               b"  <game><path>./Name Before.zip</path></game>\r\n</gameList>"))

        dat_path = self.ops / "dat-fixture/manifest.json"
        dat_manifest = json.loads(dat_path.read_text(encoding="utf-8"))
        dat_manifest["items"].append({
            "system_id": "famicom", "source_path": "roms/famicom/Name Before.zip",
            "status": "full_dat_match", "matching_dat_records": 1,
            "archive_member_count": 1, "non_rom_member_count": 0,
            "payload_member": "Name Before.nes", "suggested_container_name": "Name After.zip",
            "dat_rom_name": "Name After.nes", "dat_sha256": "d" * 64,
            "payload_size_bytes": 16, "payload_crc32": "00000000",
            "payload_md5": "e" * 32, "payload_sha1": "f" * 40,
            "payload_sha256": payload_sha,
        })
        dat_path.write_text(json.dumps(dat_manifest), encoding="utf-8")
        verify_path = self.ops / "verify-fixture/manifest.json"
        verify_manifest = json.loads(verify_path.read_text(encoding="utf-8"))
        verify_manifest["items"].append({
            "path": str(source), "status": "ok", "sha256": source_sha,
            "size_bytes": source.stat().st_size, "member_count": 1, "encrypted_member_count": 0,
        })
        verify_path.write_text(json.dumps(verify_manifest), encoding="utf-8")

        image_dir = self.root / "media/famicom/cover-front"
        image_dir.mkdir(parents=True)
        image = image_dir / "Name Before.png"
        image.write_bytes(b"invalid image")
        blocked = variant_organization.plan_dat_name_alignment(
            "famicom", "inventory-fixture", "dat-fixture", "verify-fixture", self.api)
        self.assertEqual(blocked["status"], "no_eligible_candidates")
        blocked_manifest = json.loads(Path(blocked["manifest"]).read_text())
        self.assertIn("associated_media_validation_failed",
                      [item["reason"] for item in blocked_manifest["blockers"]])
        self.assertTrue(source.is_file())
        from PIL import Image
        Image.new("RGB", (8, 8), "red").save(image)
        image_bytes = image.read_bytes()

        result = variant_organization.plan_dat_name_alignment(
            "famicom", "inventory-fixture", "dat-fixture", "verify-fixture", self.api)
        self.assertEqual(result["rom_renames"], 1)
        self.assertEqual(result["media_renames"], 2)
        self.assertEqual(result["gamelist_files"], 1)
        image.write_bytes(b"modified after planning")
        with self.assertRaisesRegex(ValueError, "Mídia associada mudou"):
            variant_organization.apply(result["operation_id"], True, self.api)
        self.assertTrue(source.is_file())
        image.write_bytes(image_bytes)
        applied = variant_organization.apply(result["operation_id"], True, self.api)
        self.assertEqual(applied["status"], "completed_verified")
        self.assertTrue((self.rom_root / "Name After.zip").is_file())
        self.assertTrue((media_dir / "Name After.txt").is_file())
        self.assertEqual((image_dir / "Name After.png").read_bytes(), image_bytes)
        self.assertIn(b"./Name After.zip", self.gamelist.read_bytes())
        self.assertEqual(variant_organization.verify(result["operation_id"], self.api)["status"], "verified")
        rolled_back = variant_organization.rollback(result["operation_id"], True, self.api)
        self.assertEqual(rolled_back["status"], "rolled_back_verified")
        self.assertTrue(source.is_file())
        self.assertEqual(media.read_bytes(), payload)
        self.assertEqual(image.read_bytes(), image_bytes)
        self.assertEqual(self.gamelist.read_bytes(), before_xml.replace(
            b"</gameList>", b"  <game><path>./Name Before.zip</path></game>\r\n</gameList>"))

    def test_apply_rejects_legacy_plan_with_unvalidated_image_before_moving_rom(self):
        result = self._plan()
        manifest_path = self.ops / result["operation_id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["media_items"] = [{"source": str(self.root / "old.png"),
                                    "destination": str(self.root / "new.png")}]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "validação integral"):
            variant_organization.apply(result["operation_id"], True, self.api)
        self.assertTrue(self.source.is_file())

    def test_image_validation_rejects_format_mismatch_and_animated_images(self):
        from PIL import Image
        image = self.root / "wrong.png"
        Image.new("RGB", (4, 4), "red").save(image, format="JPEG")
        with self.assertRaisesRegex(ValueError, "Formato divergente"):
            variant_organization._validate_image_file(image, self.api["sha256_file"])
        image = self.root / "animated.gif"
        first = Image.new("RGB", (4, 4), "red")
        second = Image.new("RGB", (4, 4), "blue")
        first.save(image, save_all=True, append_images=[second], duration=100)
        with self.assertRaisesRegex(ValueError, "animada"):
            variant_organization._validate_image_file(image, self.api["sha256_file"])

    def test_user_attested_translation_plan_requires_unique_dat_base_name_and_size(self):
        import hashlib
        import xml.etree.ElementTree as ET
        import zlib

        translated_payload = b"T" * 20
        good_source = self.rom_root / "Translation Demo (USA) (Translated PtBr).zip"
        with zipfile.ZipFile(good_source, "w") as archive:
            archive.writestr("Translation Demo (USA).nes", translated_payload)
        blocked_source = self.rom_root / "Unknown Title (USA) (Translated PtBr).zip"
        with zipfile.ZipFile(blocked_source, "w") as archive:
            archive.writestr("Unknown Title (USA).nes", translated_payload)

        good_dat = self.root / "work/translation-base.dat"
        good_dat.parent.mkdir(parents=True)
        base_payload = b"B" * len(translated_payload)
        dat_xml = (
            '<datafile><game name="Translation Demo (USA)"><rom '
            'name="Translation Demo (USA).nes" size="20" '
            f'crc="{zlib.crc32(base_payload) & 0xffffffff:08x}" '
            f'md5="{hashlib.md5(base_payload).hexdigest()}" '
            f'sha1="{hashlib.sha1(base_payload).hexdigest()}" '
            f'sha256="{hashlib.sha256(base_payload).hexdigest()}"/>'
            '</game></datafile>'
        ).encode("utf-8")
        good_dat.write_bytes(dat_xml)
        dat_manifest_path = self.ops / "dat-fixture/manifest.json"
        dat_manifest = json.loads(dat_manifest_path.read_text(encoding="utf-8"))
        dat_manifest["evidence"].update({
            "dat_path": str(good_dat), "dat_sha256": hashlib.sha256(dat_xml).hexdigest(),
        })

        def variant_row(path):
            st = path.stat()
            stored_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            return {
                "system_id": "famicom", "path": path.relative_to(self.root).as_posix(),
                "filename": path.name, "artifact_role": "catalog_supported_content_candidate",
                "size_bytes": st.st_size, "source_file_sha256": stored_sha,
                "hash_status": "hashed", "extension_supported": True,
                "device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns,
                "mode": st.st_mode & 0o777,
                "name_evidence": {"kind_candidates": [{"value": "translation"}],
                                  "language_candidates": [{"tag_candidate": "pt-BR"}]},
            }

        good_row = variant_row(good_source)
        blocked_row = variant_row(blocked_source)
        inventory_path = self.ops / "inventory-fixture/manifest.json"
        inventory_manifest = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory_manifest["items"].extend([good_row, blocked_row])
        inventory_path.write_text(json.dumps(inventory_manifest), encoding="utf-8")

        dat_manifest["items"].extend([
            {"system_id": "famicom", "source_path": str(self.root / good_row["path"]),
             "status": "no_match_retain", "matching_dat_records": 0,
             "archive_member_count": 1, "non_rom_member_count": 0,
             "payload_member": "Translation Demo (USA).nes", "payload_size_bytes": 20,
             "payload_crc32": "11111111", "payload_md5": "1" * 32,
             "payload_sha1": "2" * 40, "payload_sha256": "3" * 64},
            {"system_id": "famicom", "source_path": blocked_row["path"],
             "status": "no_match_retain", "matching_dat_records": 0,
             "archive_member_count": 1, "non_rom_member_count": 0,
             "payload_member": "Unknown Title (USA).nes", "payload_size_bytes": 20,
             "payload_crc32": "44444444", "payload_md5": "4" * 32,
             "payload_sha1": "5" * 40, "payload_sha256": "6" * 64},
        ])
        dat_manifest_path.write_text(json.dumps(dat_manifest), encoding="utf-8")

        verify_path = self.ops / "verify-fixture/manifest.json"
        verify_manifest = json.loads(verify_path.read_text(encoding="utf-8"))
        for path, row in ((good_source, good_row), (blocked_source, blocked_row)):
            verify_manifest["items"].append({
                "path": str(path), "status": "ok", "sha256": row["source_file_sha256"],
                "size_bytes": row["size_bytes"], "member_count": 1, "encrypted_member_count": 0,
            })
        verify_path.write_text(json.dumps(verify_manifest), encoding="utf-8")

        result = variant_organization.plan_user_attested_translations(
            "famicom", "inventory-fixture", "dat-fixture", "verify-fixture", "hierarchy-fixture",
            "pt-BR", "Projeto: traduções concluídas devem ficar em translation-pt-br.", self.api)
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["planned_files"], 1)
        self.assertEqual(manifest["items"][0]["destination_relative"],
                         "roms/famicom/translation-pt-br/Translation Demo (USA) (Translated PtBr).zip")
        self.assertEqual(manifest["items"][0]["base_identity_evidence"]["rom_name"],
                         "Translation Demo (USA).nes")
        self.assertEqual(manifest["items"][0]["base_identity_evidence"]["identity_limit"],
                         "patched payload does not match the DAT hash tuple")
        self.assertEqual(manifest["blockers"][0]["reason"],
                         "base_identity_not_unique_by_dat_member_name_and_size")
        self.assertTrue(good_source.is_file())
        self.assertTrue(blocked_source.is_file())

        hierarchy_path = self.ops / "hierarchy-fixture/manifest.json"
        hierarchy = json.loads(hierarchy_path.read_text(encoding="utf-8"))
        hierarchy["status"] = "prepared"
        hierarchy_path.write_text(json.dumps(hierarchy), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "precisa estar aplicada e verificada"):
            variant_organization.apply(result["operation_id"], True, self.api)
        self.assertTrue(good_source.is_file())


class TranslationBaseEvidenceTests(unittest.TestCase):
    def test_region_expansion_and_new_dat_language_annotation(self):
        row={'rom_name':'Game (Europe, Brazil) (En).sms','size_bytes':32768}
        index={(row['rom_name'].casefold(),'32768'):[row]}
        matches,method=variant_organization._translation_base_candidates('Game (Europe) (Translated PtBr).sms',32768,index)
        self.assertEqual(matches,[row]);self.assertIn('region_subset',method)
        self.assertEqual(variant_organization._translation_base_candidates('Other Game (Europe) (Translated PtBr).sms',32768,index)[0],[])

    def test_revision_and_ambiguous_editions_are_not_guessed(self):
        rows=[{'rom_name':'Game (Europe, Brazil) (En).sms','size_bytes':32768},{'rom_name':'Game (Europe, Australia) (En).sms','size_bytes':32768}]
        index={(r['rom_name'].casefold(),'32768'):[r] for r in rows}
        self.assertEqual(len(variant_organization._translation_base_candidates('Game (Europe) (Translated PtBr).sms',32768,index)[0]),2)
        self.assertEqual(variant_organization._translation_base_candidates('Game (Europe) (Rev 1) (Translated PtBr).sms',32768,index)[0],[])

    def test_sms_copier_prefix_evidence_does_not_strip_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'Game (Translated PtBr).sms'
            data=bytearray(32768+512);data[0x3ff0+512:0x3ff8+512]=b'TMR SEGA';source.write_bytes(data)
            row={'payload_size_bytes':len(data),'payload_sha256':hashlib.sha256(data).hexdigest()}
            evidence=variant_organization._sms_copier_header_evidence(source,source.name,row)
            self.assertEqual(evidence['prefix_bytes'],512);self.assertEqual(evidence['bytes_removed'],0)
            self.assertEqual(source.read_bytes(),data)
            row['payload_sha256']='0'*64
            self.assertIsNone(variant_organization._sms_copier_header_evidence(source,source.name,row))


if __name__ == "__main__":
    unittest.main()
