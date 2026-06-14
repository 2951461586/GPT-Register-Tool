import unittest
from unittest.mock import Mock, patch

from sms_tool import phone_proxy
from sms_tool.phone_reuse import PhonePool, PhoneSlot, complete_phone_verification_with_reuse


class PhoneProxyTests(unittest.TestCase):
    def test_normalize_host_port_user_pass(self):
        proxy = phone_proxy.normalize_proxy_url('sg.cliproxy.io:443:user-region-JP-sid-abc-t-5:pass')
        self.assertEqual(proxy, 'http://user-region-JP-sid-abc-t-5:pass@sg.cliproxy.io:443')

    def test_region_match_and_sid_refresh_cliproxy(self):
        base = 'http://user-region-JP-sid-yuRiTaDA-t-5:pass@sg.cliproxy.io:443'
        matched = phone_proxy.match_proxy_region(base, 'GB')
        self.assertIn('region-GB', matched)
        refreshed = phone_proxy.refresh_proxy_sid(matched)
        self.assertIn('region-GB', refreshed)
        self.assertIn('-sid-', refreshed)
        self.assertNotIn('sid-yuRiTaDA', refreshed)

    def test_region_match_and_sid_refresh_kookeey_password(self):
        base = 'http://u:pwd-JP-04061532-5m@gate-jp.kookeey.info:1000'
        matched = phone_proxy.match_proxy_region(base, 'GB')
        self.assertIn('pwd-GB-04061532-5m', matched)
        refreshed = phone_proxy.refresh_proxy_sid(matched)
        self.assertIn('pwd-GB-', refreshed)
        self.assertNotIn('04061532', refreshed)

    def test_phone_country_iso_uses_smsbower_mapping(self):
        self.assertEqual(phone_proxy.phone_country_iso('16'), 'GB')
        self.assertEqual(phone_proxy.phone_country_iso('38'), 'GH')
        self.assertEqual(phone_proxy.phone_country_iso('JP'), 'JP')

    def test_probe_scheme_detection_falls_back_to_http(self):
        calls = []
        def fake_probe(proxy, expected_country='', use_cache=True):
            calls.append(proxy)
            if proxy.startswith('socks5h://'):
                return {'ok': False, 'proxy': proxy, 'error': 'SOCKS failed'}
            return {'ok': True, 'proxy': proxy, 'country_code': expected_country, 'ip': '1.2.3.4'}
        with patch.object(phone_proxy, 'probe_proxy', side_effect=fake_probe):
            result = phone_proxy.probe_proxy_with_scheme_detection('socks5h://u:p@h:1', 'JP')
        self.assertTrue(result['ok'])
        self.assertEqual(result['proxy'], 'http://u:p@h:1')
        self.assertEqual(calls, ['socks5h://u:p@h:1', 'http://u:p@h:1'])

    def test_select_phone_proxy_matches_country_refreshes_sid(self):
        with patch.object(phone_proxy, '_configured_proxy_api_url', return_value=''), \
             patch.object(phone_proxy, 'configured_base_proxy', return_value='http://u-region-JP-sid-ABCDEFGH-t-5:p@h:1'), \
             patch.object(phone_proxy, 'probe_proxy_with_scheme_detection') as probe:
            probe.side_effect = lambda proxy, expected_country='', use_cache=True: {
                'ok': True, 'proxy': proxy, 'ip': '1.2.3.4', 'country_code': expected_country
            }
            result = phone_proxy.select_phone_proxy(country='16')
        self.assertTrue(result['ok'])
        self.assertIn('region-GB', result['proxy'])
        self.assertNotIn('sid-ABCDEFGH', result['proxy'])
        self.assertEqual(result['region'], 'GB')

    def test_fetch_proxy_from_api_rewrites_region_and_normalizes_ip_port(self):
        response = Mock(status_code=200, text='103.49.62.181:19001\n')
        response.raise_for_status = Mock()
        with patch.object(phone_proxy.requests, 'get', return_value=response) as get:
            result = phone_proxy.fetch_proxy_from_api('https://api.example/white/api?region=JP&num=1', 'GB')
        self.assertTrue(result['ok'])
        self.assertEqual(result['proxy'], 'http://103.49.62.181:19001')
        self.assertIn('region=GB', get.call_args.args[0])

    def test_select_phone_proxy_can_use_configured_api_url(self):
        with patch.object(phone_proxy, '_configured_proxy_api_url', return_value='https://api.example/white/api?region=JP&num=1'), \
             patch.object(phone_proxy, 'fetch_proxy_from_api', return_value={'ok': True, 'proxy': 'http://103.49.62.181:19001'}), \
             patch.object(phone_proxy, 'probe_proxy_with_scheme_detection', return_value={'ok': True, 'proxy': 'http://103.49.62.181:19001', 'ip': '1.2.3.4', 'country_code': 'GB'}):
            result = phone_proxy.select_phone_proxy(country='16')
        self.assertTrue(result['ok'])
        self.assertEqual(result['proxy'], 'http://103.49.62.181:19001')


class PhoneReuseProxyGateTests(unittest.TestCase):
    def test_provider_proxy_failure_stops_before_buying_number(self):
        pool = PhonePool(phones=[PhoneSlot(provider='smsbower', api_key='k', country='16', phone='', activation_id='')])
        session = Mock()
        with patch('sms_tool.phone_reuse._acquire_smsbower_number') as acquire, \
             patch('sms_tool.phone_proxy.select_phone_proxy', return_value={'ok': False, 'error': 'forbidden ip'}):
            result = complete_phone_verification_with_reuse(session, 'did', 'https://auth.openai.com/add-phone', pool, proxy='http://bad.proxy:8080')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'phone_proxy_unavailable')
        acquire.assert_not_called()


if __name__ == '__main__':
    unittest.main()
