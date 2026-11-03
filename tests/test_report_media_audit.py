"""Static regression checks for Telegram media and detailed audit identity."""
import pathlib
import unittest

CODE = (pathlib.Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")


class MediaAndAuditTests(unittest.TestCase):
    def test_reports_accept_photo_video_and_link(self):
        proof = CODE.split("async def process_report_proof(", 1)[1].split(
            '@dp.callback_query(F.data == "cancel_report")', 1)[0]
        for token in ("message.photo", "message.video", "file_id", "proof_media_type",
                      "proof_file_id", "https://", "ReportStates.confirm"):
            self.assertIn(token, proof)

    def test_evidence_persisted_and_forwarded_to_leadership(self):
        submit = CODE.split("async def send_report_confirmed(", 1)[1].split(
            "# ╔", 1)[0]
        self.assertIn('"proof_file_id": data.get("proof_file_id", "")', submit)
        forwarded = CODE.split("async def send_pending_report_to_admin(", 1)[1].split(
            "async def refresh_pending_report_messages(", 1)[0]
        self.assertIn("await bot.send_photo(", forwarded)
        self.assertIn("await bot.send_video(", forwarded)
        self.assertIn("media_kind in (\"photo\", \"video\")", forwarded)

    def test_support_media_types_and_true_storage_status(self):
        ticket = CODE.split("async def process_support_msg(", 1)[1].split(
            "# ╔", 1)[0]
        self.assertIn('"video_id": message.video.file_id if message.video else ""', ticket)
        self.assertIn("await bot.send_video(", ticket)
        self.assertIn("if ticket_id else", ticket)

    def test_panel_operations_and_delete_have_audit(self):
        self.assertIn('record_admin_activity(activity, callback.from_user.id,', CODE)
        self.assertIn('record_admin_activity("agent_deleted", callback.from_user.id,', CODE)
        self.assertIn('profile.get("telegram_username")', CODE)
        self.assertIn('profile.get("nick")', CODE)


if __name__ == "__main__":
    unittest.main()
