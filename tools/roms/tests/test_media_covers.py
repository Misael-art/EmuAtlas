import csv
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from PIL import Image
from tools.roms import media_covers as c, romtool as r


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.media=self.root/"media";self.media.mkdir()
        self.out=self.root/"outputs";self.out.mkdir();self.ops=self.out/"operations";self.ops.mkdir()
        self.module=self.root/"tools/roms/media_covers.py";self.module.parent.mkdir(parents=True);self.module.write_text("fixture binding")
        self.patch=patch.object(c,"SOURCE",self.module);self.patch.start();self.addCleanup(self.patch.stop)
        self.api=dict(PROJECT_ROOT=self.root,MEDIA_DEST_ROOT=self.media,OUTPUT_ROOT=self.out,OPS_ROOT=self.ops,dt=dt,
                      now=r.now,operation_dir=lambda key:self.ops/key,atomic_json=r.atomic_json,
                      append_event=lambda *a,**kw:None,rebuild_history_index=lambda:None,
                      path_snapshot=r.path_snapshot,sha256_file=r.sha256_file,reject_symlink_components=r.reject_symlink_components,
                      frontend_processes_running=lambda:{"retrofe":[],"esde":[]},
                      write_csv_atomic=r.write_csv_atomic,relative_media_symlink=r.relative_media_symlink,media_link_matches=r.media_link_matches)

    def image(self,name="sms/cover-front/Test.jpg",color="red"):
        path=self.media/name;path.parent.mkdir(parents=True,exist_ok=True)
        with Image.new("RGB",(80,50),color) as image:
            image.paste("blue",(0,0,36,50));image.paste("gray",(36,0,44,50));image.save(path)
        return path

    def prepare(self,paths=None):
        paths=paths or [self.image()]
        audit=c.audit(NS(system_id=None),self.api)
        review=self.root/"reviews.json"
        review.write_text(json.dumps(dict(items=[dict(source=p.relative_to(self.media).as_posix(),sha256=r.sha256_file(p),
            classification="full-cover",front_box=[44,0,80,50],evidence="Visual inspection confirms back at left, spine in middle, complete front at right") for p in paths])))
        result=c.plan(NS(audit_operation_id=audit["operation_id"],reviews_json=str(review)),self.api)
        manifest=json.loads((self.ops/result["operation_id"]/"manifest.json").read_text())
        return result["operation_id"],manifest

    def apply_one(self,opid,item):
        return c.apply(NS(operation_id=opid,item_id=item["item_id"],apply=True),self.api)

    def test_audit_never_moves_or_crops_horizontal_front(self):
        source=self.image("n64/cover-front/Horizontal.png");before=source.read_bytes()
        result=c.audit(NS(system_id=None),self.api)
        row=json.loads((self.ops/result["operation_id"]/"manifest.json").read_text())["items"][0]
        self.assertEqual(row["classification"],"landscape_front_needs_review")
        self.assertEqual(before,source.read_bytes())
        self.assertFalse((self.media/"n64/cover-full").exists())

    def test_review_hash_and_bounds_are_required(self):
        source=self.image();opid,manifest=self.prepare([source]);source.write_bytes(b"changed")
        with self.assertRaises(ValueError):self.apply_one(opid,manifest["items"][0])
        with self.assertRaises(ValueError):c._front(self.image("sms/cover-front/Other.png"),[0,0,800,50])

    def test_split_preserves_original_exactly_and_crop_pixels(self):
        source=self.image();original=source.read_bytes();opid,manifest=self.prepare([source]);item=manifest["items"][0]
        expected,_=c._front(source,item["front_proof"]["front_box"])
        self.assertEqual(self.apply_one(opid,item)["status"],"verified")
        self.assertEqual((self.media/item["full"]).read_bytes(),original)
        self.assertEqual((self.media/item["front"]).read_bytes(),expected)
        self.assertTrue(source.is_symlink());self.assertFalse(Path(source.readlink()).is_absolute())
        self.assertEqual(c.verify(NS(operation_id=opid),self.api)["verified"],1)
        self.assertFalse(list((self.ops/opid/"staging").glob("*.png")))

    def test_duplicate_payloads_share_active_files_with_reversible_originals(self):
        first=self.image("sms/cover-front/A.png");second=self.image("sms/cover-front/B.png")
        original=first.read_bytes();opid,manifest=self.prepare([first,second])
        for item in manifest["items"]:self.apply_one(opid,item)
        self.assertTrue(second.is_symlink())
        fulls=[p for p in (self.media/"sms/cover-full").iterdir() if not p.is_symlink()]
        self.assertEqual(len(fulls),1)
        c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertEqual(first.read_bytes(),original);self.assertEqual(second.read_bytes(),original)
        self.assertFalse(first.is_symlink());self.assertFalse(second.is_symlink())

    def test_frontends_and_code_changes_block_apply(self):
        opid,manifest=self.prepare()
        self.api["frontend_processes_running"]=lambda:{"retrofe":[123]}
        with self.assertRaises(RuntimeError):self.apply_one(opid,manifest["items"][0])
        self.api["frontend_processes_running"]=lambda:{}
        self.module.write_text("changed code")
        with self.assertRaises(ValueError):self.apply_one(opid,manifest["items"][0])

    def test_rollback_rejects_changed_front_before_any_mutation(self):
        source=self.image();opid,manifest=self.prepare([source]);item=manifest["items"][0];self.apply_one(opid,item)
        front=self.media/item["front"];front.write_bytes(b"foreign change")
        with self.assertRaises(ValueError):c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertTrue(source.is_symlink());self.assertTrue((self.media/item["full"]).is_file())

    def test_interrupted_publication_rolls_back_without_losing_original(self):
        source=self.image();original=source.read_bytes();opid,manifest=self.prepare([source])
        with patch.object(c.os,"link",side_effect=OSError("simulated publication failure")):
            with self.assertRaises(OSError):self.apply_one(opid,manifest["items"][0])
        c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertEqual(source.read_bytes(),original)
        self.assertFalse(list((self.ops/opid/"staging").glob("*.png")))

    def test_verifier_raises_instead_of_reporting_success_for_changed_payload(self):
        opid,manifest=self.prepare();item=manifest["items"][0];self.apply_one(opid,item)
        (self.media/item["front"]).write_bytes(b"broken")
        with self.assertRaises(ValueError):c.verify(NS(operation_id=opid),self.api)

    def test_plan_refuses_destination_collision_and_traversal(self):
        source=self.image();self.image("sms/cover-full/Test.jpg")
        with self.assertRaises(FileExistsError):self.prepare([source])
        with self.assertRaises(ValueError):c._media(self.api,"../cover-front/a.png")

    def test_animated_and_high_depth_images_are_preserved(self):
        p=self.media/"sms/cover-front/16bit.png";p.parent.mkdir(parents=True)
        Image.new("I;16",(80,50)).save(p)
        audit=c.audit(NS(system_id=None),self.api)
        row=json.loads((self.ops/audit["operation_id"]/"manifest.json").read_text())["items"][0]
        self.assertEqual(row["status"],"blocked_preserved")

    def high_depth_front_review(self):
        source = self.media / "gbc/cover-front/Reviewed.png"
        source.parent.mkdir(parents=True)
        with Image.new("I;16", (80, 50)) as image:
            image.save(source)
        audit = c.audit(NS(system_id=None), self.api)
        request = self.root / "high-depth-review.json"
        request.write_text(json.dumps({"items": [{"source": "gbc/cover-front/Reviewed.png",
                           "sha256": r.sha256_file(source), "classification": "front-cover",
                           "evidence": "Visual inspection confirms the complete legitimate front. Keep all original high-depth bytes."}]}))
        review = c.review(NS(audit_operation_id=audit["operation_id"], reviews_json=str(request)), self.api)
        (self.ops / "history-index.csv").write_text("operation_id,kind,status,created_at\n" + review["operation_id"]
            + ",media_cover_review,completed_verified,2026-11-14\n")
        self.api["media_session"] = r.media_session
        return source

    def test_reviewed_high_depth_front_uses_native_read_only_validation(self):
        source = self.high_depth_front_review()
        before = source.read_bytes()
        native = {"frame_count": 1, "frames": [{"format": "PNG", "depth": "16"}]}
        with patch.object(r.media_session, "_validate", return_value=native) as validate:
            result = c.audit(NS(system_id=None), self.api)
        row = json.loads((self.ops / result["operation_id"] / "manifest.json").read_text())["items"][0]
        validate.assert_called_once_with(source, self.api)
        self.assertEqual(row["classification"], "confirmed_front_cover")
        self.assertEqual(row["status"], "preserved")
        self.assertFalse(row["needs_review"])
        self.assertEqual(row["stored_bit_depth"], 16)
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(ValueError):
            c._front(source, [40, 0, 80, 50])

    def test_reviewed_high_depth_front_keeps_native_decode_failures_visible(self):
        source = self.high_depth_front_review()
        with patch.object(r.media_session, "_validate", side_effect=ValueError("native decode failed")):
            result = c.audit(NS(system_id=None), self.api)
        row = json.loads((self.ops / result["operation_id"] / "manifest.json").read_text())["items"][0]
        self.assertEqual(row["status"], "blocked_preserved")
        self.assertTrue(row["needs_review"])
        self.assertIn("native decode failed", row["reason"])
        self.assertTrue(source.exists())

    def test_high_depth_validation_rejects_a_write_with_restored_mtime(self):
        source = self.high_depth_front_review()
        before = source.read_bytes()
        stat = source.stat()
        def concurrent_write(path, api):
            path.write_bytes(before)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return {"frame_count": 1, "frames": [{"format": "PNG", "depth": "16"}]}
        with patch.object(r.media_session, "_validate", side_effect=concurrent_write):
            result = c.audit(NS(system_id=None), self.api)
        row = json.loads((self.ops / result["operation_id"] / "manifest.json").read_text())["items"][0]
        self.assertEqual(row["status"], "blocked_preserved")
        self.assertIn("mudou durante diagnóstico", row["reason"])

    def test_reviewed_high_depth_front_preserves_the_pixel_limit(self):
        self.high_depth_front_review()
        with patch.object(c, "MAX_PIXELS", 100), patch.object(r.media_session, "_validate") as validate:
            result = c.audit(NS(system_id=None), self.api)
        validate.assert_not_called()
        row = json.loads((self.ops / result["operation_id"] / "manifest.json").read_text())["items"][0]
        self.assertEqual(row["status"], "blocked_preserved")
        self.assertIn("limite de 32 megapixels", row["reason"])

    def test_all_platforms_are_scanned_and_aliases_not_decoded_twice(self):
        self.image("sms/cover-front/A.png");p=self.image("snes/cover/B.png")
        (p.parent/"alias.png").symlink_to("B.png")
        audit=c.audit(NS(system_id=None),self.api)
        self.assertEqual(audit["summary"]["files"],2);self.assertEqual(audit["summary"]["aliases"],1)

    def test_physical_media_can_receive_a_reviewed_arcade_cabinet_role(self):
        source = self.image("model2/physicalmedia/Arcade.png")
        audit = c.audit(NS(system_id=["model2"]), self.api)
        request = self.root / "cabinet-review.json"
        request.write_text(json.dumps({"items": [{"source": "model2/physicalmedia/Arcade.png",
                           "sha256": r.sha256_file(source), "classification": "not-cover",
                           "target_media_type": "cabinet", "evidence": "Visual review identifies a complete arcade cabinet with monitor and controls."}]}))
        result = c.review(NS(audit_operation_id=audit["operation_id"], reviews_json=str(request)), self.api)
        manifest = json.loads((self.ops / result["operation_id"] / "manifest.json").read_text())
        self.assertEqual(manifest["items"][0]["target_media_type"], "cabinet")
        self.assertTrue(source.is_file())

    def test_letterbox_is_diagnostic_and_does_not_authorize_crop(self):
        image=Image.new("RGB",(100,100),"white")
        image.paste("blue",(0,25,100,75))
        self.assertEqual(c._content_bbox(image.convert("RGBA")),[0,25,100,75])

    def test_reviewed_front_is_reused_by_sha_and_does_not_rename(self):
        source=self.image("n64/cover-front/Wide.png");original=source.read_bytes()
        audit=c.audit(NS(system_id=None),self.api)
        review_path=self.root/"front-review.json"
        review_path.write_text(json.dumps(dict(items=[dict(source="n64/cover-front/Wide.png",sha256=r.sha256_file(source),
            classification="front-cover",evidence="Visual inspection confirms a legitimate landscape front, no spine or back") ])))
        result=c.review(NS(audit_operation_id=audit["operation_id"],reviews_json=str(review_path)),self.api)
        (self.ops/"history-index.csv").write_text("operation_id,kind,status,created_at\n"+result["operation_id"]+",media_cover_review,completed_verified,2026-10-27\n")
        after=c.audit(NS(system_id=None),self.api)
        row=json.loads((self.ops/after["operation_id"]/"manifest.json").read_text())["items"][0]
        self.assertEqual(row["classification"],"confirmed_front_cover");self.assertFalse(row["needs_review"])
        self.assertEqual(source.read_bytes(),original)

    def test_relative_paths_survive_project_relocation(self):
        source=self.image();opid,manifest=self.prepare([source]);original=source.read_bytes()
        other=self.root.parent/(self.root.name+"-moved");self.root.rename(other)
        self.addCleanup(lambda:other.rename(self.root))
        self.api.update(PROJECT_ROOT=other,MEDIA_DEST_ROOT=other/"media",OUTPUT_ROOT=other/"outputs",OPS_ROOT=other/"outputs/operations")
        self.api["operation_dir"]=lambda key:other/"outputs/operations"/key
        with patch.object(c,"SOURCE",other/"tools/roms/media_covers.py"):
            self.apply_one(opid,manifest["items"][0])
            c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertEqual((other/"media/sms/cover-front/Test.jpg").read_bytes(),original)

    def test_exif_rotation_uses_oriented_pixel_coordinates(self):
        p=self.media/"sms/cover-front/Rotated.jpg";p.parent.mkdir(parents=True)
        with Image.new("RGB",(50,80),"green") as im:
            exif=Image.Exif();exif[274]=6;im.save(p,exif=exif)
        with c._pixels(p) as (image,metadata):self.assertEqual(image.size,(80,50))
        data,proof=c._front(p,[44,0,80,50]);self.assertEqual(proof["dimensions"],[36,50])

    def test_rollback_resumes_after_original_was_already_restored(self):
        source=self.image();original=source.read_bytes();opid,manifest=self.prepare([source]);item=manifest["items"][0]
        self.apply_one(opid,item)
        source.unlink();front=self.media/item["front"];retained=self.ops/opid/"rollback/applied"/(item["item_id"]+".png")
        retained.parent.mkdir(parents=True);front.rename(retained);(self.media/item["full"]).rename(source)
        c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertEqual(source.read_bytes(),original)

    def test_modified_rollback_event_cannot_escape_project(self):
        opid,manifest=self.prepare();self.apply_one(opid,manifest["items"][0])
        path=self.ops/opid/"manifest.json";manifest=json.loads(path.read_text())
        manifest["items"][0]["events"].append(dict(kind="create_alias",path="../../outside",target="file"));path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):c.rollback(NS(operation_id=opid,rollback=True),self.api)

    def test_review_path_cannot_escape_project_via_parent(self):
        with self.assertRaises(ValueError):c._review_file(self.api,"../outside-review.json")

    def test_updated_tool_requires_evidence_attestation_and_keeps_old_manifest(self):
        source=self.image();original=source.read_bytes();opid,manifest=self.prepare([source]);self.apply_one(opid,manifest["items"][0])
        old_manifest=(self.ops/opid/"manifest.json").read_bytes();self.module.write_text("reviewed source update")
        with self.assertRaises(ValueError):c.rollback(NS(operation_id=opid,rollback=True),self.api)
        result=c.attest(NS(operation_id=opid,reason="Reviewed path validation fix; crop algorithm and every byte/pixel remain identical"),self.api)
        self.assertEqual((self.ops/opid/"manifest.json").read_bytes(),old_manifest)
        (self.ops/"history-index.csv").write_text("operation_id,kind,status,created_at\n"+result["operation_id"]+",media_cover_attestation,completed_verified,2026-10-27\n")
        c.rollback(NS(operation_id=opid,rollback=True),self.api)
        self.assertEqual(source.read_bytes(),original)


if __name__=="__main__":unittest.main()
