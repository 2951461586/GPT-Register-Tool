import base64
import json
import unittest
from sms_tool import workspace_scan


def _jwt(payload):
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"e30.{body}.sig"

class WorkspaceScanTests(unittest.TestCase):
    def test_parse_fallback_ids_dedupes(self):
        self.assertEqual(workspace_scan.parse_workspace_fallback_ids('ws1, ws2\nws1'), ['ws1', 'ws2'])

    def test_detects_workspace_deactivated_markers(self):
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1'},
            fetch_auth_session_func=lambda account, **kwargs: {'error': {'code': 'workspace_deactivated'}},
        )
        self.assertEqual(result['status'], 'workspace_deactivated')
        self.assertFalse(result['ok'])

    def test_workspace_ok_from_session_account(self):
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1'},
            target_workspace_id='ws-1',
            fetch_auth_session_func=lambda account, **kwargs: {'account': {'id': 'ws-1'}},
        )
        self.assertEqual(result['status'], 'workspace_ok')
        self.assertTrue(result['ok'])

    def test_transport_error_is_inconclusive(self):
        def fetcher(account, **kwargs):
            raise RuntimeError("Failed to perform, curl: (56) Recv failure: Connection was reset")

        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1'},
            fetch_auth_session_func=fetcher,
        )
        self.assertEqual(result['status'], 'workspace_check_inconclusive')
        self.assertFalse(result['ok'])
        self.assertIn('curl: (56)', result['error'])
        self.assertTrue(result.get('inconclusive'))

    def test_account_type_prefers_current_free_signal_over_stale_pro(self):
        self.assertEqual(
            workspace_scan._account_type(
                {
                    "account_type": "pro",
                    "subscription_type": "free",
                }
            ),
            "free",
        )

    def test_token_account_id_is_not_promoted_to_workspace_id(self):
        result = workspace_scan.inspect_workspace(
            {
                'email': 'a@example.com',
                'cookie_header': 's=1',
                'subscription_type': 'free',
                'access_token': _jwt(
                    {
                        'https://api.openai.com/auth': {
                            'chatgpt_plan_type': 'free',
                            'chatgpt_account_id': 'ws-token',
                        }
                    }
                ),
            },
            fetch_auth_session_func=lambda account, **kwargs: {'WARNING_BANNER': 'banner'},
        )
        self.assertEqual(result['status'], 'workspace_ok')
        self.assertEqual(result['account_type_before'], 'free')
        self.assertEqual(result['actual_workspace_id'], '')
        self.assertEqual(result['workspace_name'], '')

    def test_switches_to_fallback_workspace_after_disabled_current(self):
        calls = []
        fetch_count = {'n': 0}
        def selector(cookie, workspace_id, **kwargs):
            calls.append(workspace_id)
            return {'ok': True, 'workspace_id': workspace_id}
        def fetcher(account, **kwargs):
            fetch_count['n'] += 1
            if fetch_count['n'] == 1:
                return {'error': {'code': 'workspace_deactivated'}}
            account['account_id'] = calls[-1] if calls else 'old'
            return {'account': {'id': account['account_id']}}
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1', 'account_id': 'old'},
            fallback_workspace_ids=['ws-new'],
            auto_switch=True,
            fetch_auth_session_func=fetcher,
            select_workspace_func=selector,
        )
        self.assertEqual(result['status'], 'workspace_switched')
        self.assertEqual(result['switched_workspace_id'], 'ws-new')

    def test_k12_disabled_auto_switches_to_available_workspace(self):
        calls = []
        fetch_count = {'n': 0}
        session_body = {
            'account': {'id': 'k12-old', 'disabled': True, 'name': 'Old K12'},
            'accounts': [
                {'id': 'k12-old', 'name': 'Old K12', 'disabled': True},
                {'id': 'team-new', 'name': 'Available Team', 'plan_type': 'team'},
                {'id': 'personal', 'name': 'Personal', 'is_personal': True},
            ],
        }
        def selector(cookie, workspace_id, **kwargs):
            calls.append(workspace_id)
            return {'ok': True, 'workspace_id': workspace_id}
        def fetcher(account, **kwargs):
            fetch_count['n'] += 1
            if fetch_count['n'] == 1:
                return session_body
            return {'account': {'id': calls[-1], 'name': 'Available Team'}, 'accounts': session_body['accounts']}
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1', 'k12_status': 'k12_joined'},
            fetch_auth_session_func=fetcher,
            select_workspace_func=selector,
        )
        self.assertEqual(result['status'], 'workspace_switched')
        self.assertEqual(result['switched_workspace_id'], 'team-new')

    def test_k12_disabled_without_available_workspace_falls_back_free(self):
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1', 'k12_status': 'k12_joined'},
            fetch_auth_session_func=lambda account, **kwargs: {'account': {'id': 'k12-old', 'disabled': True}, 'accounts': []},
            select_workspace_func=lambda *args, **kwargs: {'ok': False},
        )
        self.assertEqual(result['status'], 'workspace_fallback_free')
        self.assertEqual(result['account_type_after'], 'free')

    def test_target_mismatch_without_auto_switch_marks_not_target(self):
        result = workspace_scan.inspect_workspace(
            {'email': 'a@example.com', 'cookie_header': 's=1'},
            target_workspace_id='ws-target',
            fetch_auth_session_func=lambda account, **kwargs: {'account': {'id': 'personal'}},
        )
        self.assertEqual(result['status'], 'workspace_switch_failed')

if __name__ == '__main__':
    unittest.main()
