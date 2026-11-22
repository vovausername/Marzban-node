import unittest
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch

import routing_sync as rs
import xray_hot_reload as hot


class RoutingSyncTest(unittest.TestCase):
    def setUp(self):
        self.base = {'rules': [{'inboundTag': ['API_INBOUND', 'API_INBOUND_LOCAL'], 'outboundTag': 'API'},
                               {'ruleTag': 'normal', 'network': 'tcp,udp', 'outboundTag': 'direct'}]}
        self.core = NS(version='26.9.9', started=True, config={'routing': deepcopy(self.base),
                                                               'outbounds': [{'tag': 'direct'}, {'tag': 'special'}]})
        self.desired = {'rules': [{'ruleTag': 'marzban-squad-1-rule-1', 'user': ['1.alice'],
                                   'outboundTag': 'special'}, self.base['rules'][1]]}
        self.base_hash = rs.digest(rs.normalize(self.base, True))
        self.hash = rs.digest(self.desired)

    def test_apply_then_hash_only_poll_and_remove(self):
        self.assertTrue(rs.sync(self.core, self.hash, self.base_hash)['needs_config'])
        with patch.object(hot, '_run_xray_api') as run:
            result = rs.sync(self.core, self.hash, self.base_hash, self.desired)
            self.assertEqual(result['applied_hash'], self.hash)
            self.assertEqual(self.core.config['routing']['rules'][0], self.base['rules'][0])
            rs.sync(self.core, self.hash, self.base_hash)
            self.assertEqual(run.call_count, 1)
            rs.sync(self.core, self.base_hash, self.base_hash, rs.normalize(self.base))
            self.assertEqual(self.core.config['routing'], self.base)

    def test_failed_apply_preserves_tracked_config(self):
        old = deepcopy(self.core.config)
        with patch.object(hot, '_run_xray_api', side_effect=hot.HotReloadError('invalid')):
            with self.assertRaises(hot.HotReloadError):
                rs.sync(self.core, self.hash, self.base_hash, self.desired)
        self.assertEqual(self.core.config, old)

    def test_old_core_and_base_mismatch_rejected_without_cli(self):
        with patch.object(hot, '_run_xray_api') as run:
            self.core.version = '26.7.28'
            with self.assertRaises(ValueError):
                rs.sync(self.core, self.hash, self.base_hash, self.desired)
            self.core.version = '26.9.9'
            with self.assertRaises(ValueError):
                rs.sync(self.core, self.hash, 'wrong', self.desired)
            run.assert_not_called()

    def test_repairs_stale_base_with_already_loaded_outbound(self):
        self.core.config['outbounds'].append({'tag': 'ai-usa'})
        new_routing = {'rules': [
            {'ruleTag': 'marzban-squad-2-rule-1', 'user': ['1.alice'], 'outboundTag': 'ai-usa'},
            {'ruleTag': 'marzban-squad-1-rule-1', 'user': ['1.alice'], 'outboundTag': 'special'},
            dict(self.base['rules'][1], ruleTag='updated'),
        ]}
        base_hash = rs.digest(rs.normalize(new_routing, True))
        desired_hash = rs.digest(new_routing)
        with patch.object(hot, '_run_xray_api') as run:
            from fastapi import HTTPException
            from rest_service import Service
            service = NS(core=self.core, match_session_id=lambda _: None)
            with patch('rest_service.XRAY_HOT_RELOAD_ENABLED', True):
                with self.assertRaises(HTTPException) as caught:
                    Service.sync_routing(service, 'session', desired_hash, base_hash,
                                         routing=None, allow_base_repair=True, observed_hash=None)
                self.assertEqual(caught.exception.status_code, 409)
                detail = caught.exception.detail
                self.assertEqual(detail['code'], 'base_routing_mismatch')
                self.assertEqual(detail['current_base_hash'], self.base_hash)
                self.assertEqual(detail['expected_base_hash'], base_hash)
                run.assert_not_called()
                result = rs.sync(self.core, desired_hash, base_hash, new_routing,
                                 observed_hash=detail['observed_hash'])
            run.assert_called_once()
        self.assertEqual(result['applied_hash'], desired_hash)
        self.assertEqual(self.core.config['routing']['rules'], [self.base['rules'][0]] + new_routing['rules'])

    def test_repair_rejects_changed_routing_and_unsupported_fields(self):
        new_routing = dict(self.desired, domainStrategy='IPIfNonMatch')
        base_hash = rs.digest(rs.normalize(new_routing, True))
        desired_hash = rs.digest(new_routing)
        observed_hash = rs.digest(rs.normalize(self.core.config['routing']))
        with patch.object(hot, '_run_xray_api') as run:
            with self.assertRaisesRegex(ValueError, 'Only rules and balancers'):
                rs.sync(self.core, desired_hash, base_hash, new_routing, observed_hash=observed_hash)
            self.core.config['routing']['rules'].extend(self.desired['rules'][:1])
            with self.assertRaisesRegex(ValueError, 'Routing changed since'):
                rs.sync(self.core, desired_hash, base_hash, new_routing, observed_hash=observed_hash)
            run.assert_not_called()

    def test_payload_tampering_and_unknown_outbound(self):
        with patch.object(hot, '_run_xray_api') as run:
            with self.assertRaises(ValueError):
                rs.sync(self.core, 'wrong', self.base_hash, self.desired)
            self.core.config['outbounds'] = []
            with self.assertRaises(ValueError):
                rs.sync(self.core, self.hash, self.base_hash, self.desired)
            run.assert_not_called()

    def test_reconnect_hot_reload_applies_managed_rules_without_restart(self):
        desired_config = deepcopy(self.core.config)
        desired_config['routing'] = {'rules': [self.base['rules'][0]] + self.desired['rules']}
        stats = {'added': 0, 'removed': 0, 'unchanged': 0, 'skipped_tags': [], 'pending_users': {}}
        with patch.object(hot, 'XRAY_HOT_RELOAD_ENABLED', True), \
                patch.object(hot, '_run_xray_api') as run, \
                patch.object(hot, 'reconcile_users', return_value=stats):
            self.assertIsNotNone(hot.hot_reload(self.core, desired_config))
            self.assertEqual(self.core.config, desired_config)
            self.assertEqual(run.call_args.args[0][0], 'adrules')

    def test_old_core_ordinary_hot_reload_does_not_call_routing_api(self):
        self.core.version = '26.7.28'
        stats = {'added': 0, 'removed': 0, 'unchanged': 0, 'skipped_tags': [], 'pending_users': {}}
        with patch.object(hot, 'XRAY_HOT_RELOAD_ENABLED', True), \
                patch.object(hot, '_cli_supports_user_ops', return_value=True), \
                patch.object(rs, 'sync') as sync, \
                patch.object(hot, 'reconcile_users', return_value=stats) as users:
            self.assertEqual(hot.hot_reload(self.core, deepcopy(self.core.config)), stats)
            users.assert_called_once()
            sync.assert_not_called()

    def test_old_core_does_not_advertise_routing_feature(self):
        from rest_service import Service
        import rest_service
        self.core.version = '26.7.28'
        with patch.object(rest_service, 'XRAY_HOT_RELOAD_ENABLED', True), \
                patch.object(hot, '_cli_supports_user_ops', return_value=True):
            features = Service.features.fget(NS(core=self.core))
        self.assertIn('sync-users', features)
        self.assertNotIn('sync-routing', features)
