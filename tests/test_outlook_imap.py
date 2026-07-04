import unittest
from email.message import EmailMessage

from sms_tool import outlook_imap


class OutlookImapTests(unittest.TestCase):
    def test_html_message_is_normalized_to_graph_shape(self):
        msg = EmailMessage()
        msg["Subject"] = "Your temporary ChatGPT verification code"
        msg["To"] = "target@hotmail.com"
        msg["Date"] = "Sat, 04 Jul 2026 11:00:00 +0800"
        msg.set_content("<html><body>Your code is <b>123456</b></body></html>", subtype="html")

        shaped = outlook_imap.imap_message_to_graph_shape("Junk", b"42", msg.as_bytes())

        self.assertEqual(shaped["_source"], "outlook_imap")
        self.assertEqual(shaped["_folder"], "Junk")
        self.assertIn("123456", shaped["body"]["content"])
        self.assertEqual(shaped["toRecipients"][0]["emailAddress"]["address"], "target@hotmail.com")


if __name__ == "__main__":
    unittest.main()
