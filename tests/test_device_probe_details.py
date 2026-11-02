"""Probe diagnostics use fake devices only; no optical device is opened."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery as r


def fixed_sense(key, asc, ascq):
    sense = bytearray(18)
    sense[0], sense[2], sense[7], sense[12], sense[13] = 0x70, key, 10, asc, ascq
    return bytes(sense)


def diagnostic_fields(error):
    error.winerror = 1167
    error.errno = 5
    error.cdb_hex = '000000000000'
    error.scsi_status = 2
    error.requested_bytes = 0
    error.transferred_bytes = 0
    error.transferred_bytes_reliable = False
    error.duration_seconds = 0.217
    return error


class ProbeDetailsTests(unittest.TestCase):
    def probe(self, error=None):
        device = Mock()
        device.command.return_value = b''
        device.command.side_effect = error
        with patch.object(r, 'Disc', return_value=device) as constructor:
            result = r.probe_disc('F:')
        constructor.assert_called_once_with('F:')
        # The readiness probe must issue only TEST UNIT READY, never read,
        # write, finalize, eject, or another state-changing command.
        device.command.assert_called_once_with([0, 0, 0, 0, 0, 0], 0)
        device.close.assert_called_once_with()
        return result

    def assert_numeric_details(self, result, error):
        details = result['details']
        self.assertEqual(details['type'], type(error).__name__)
        self.assertEqual(details['message'], str(error))
        for key in ('winerror', 'errno', 'scsi_status', 'requested_bytes', 'transferred_bytes'):
            self.assertEqual(details[key], getattr(error, key))
            self.assertIs(type(details[key]), int)
        self.assertIs(details['transferred_bytes_reliable'], False)
        self.assertIs(type(details['duration_seconds']), float)
        self.assertEqual(details['duration_seconds'], 0.217)
        self.assertEqual(details['cdb_hex'], '000000000000')
        # Exact diagnostic values must survive the JSON status-log boundary.
        self.assertEqual(json.loads(json.dumps(result))['details'], details)

    def test_ready_has_success_state_and_closes_device(self):
        result = self.probe()
        self.assertEqual(result['state'], 'ready')
        self.assertIn('Disc detected', result['message'])
        self.assertNotIn('details', result)

    def test_becoming_ready_keeps_loading_sense_and_transport_details(self):
        error = diagnostic_fields(r.sense_error(fixed_sense(2, 4, 1)))
        result = self.probe(error)
        self.assertEqual(result['state'], 'loading')
        self.assertEqual((result['details']['sense_key'], result['details']['asc'], result['details']['ascq']), (2, 4, 1))
        self.assertEqual(result['details']['sense_hex'], fixed_sense(2, 4, 1).hex())
        self.assert_numeric_details(result, error)

    def test_reset_is_loading_while_original_reset_error_remains_in_details(self):
        error = diagnostic_fields(r.sense_error(fixed_sense(6, 0x29, 0)))
        result = self.probe(error)
        self.assertEqual(result['state'], 'loading')
        self.assertIn('reset', result['message'])
        self.assertEqual(result['details']['asc'], 0x29)
        self.assertEqual(result['details']['type'], 'DiscChanged')
        self.assert_numeric_details(result, error)

    def test_closed_tray_no_medium_is_explicit_and_keeps_exact_sense(self):
        error = diagnostic_fields(r.sense_error(fixed_sense(2, 0x3a, 1)))
        result = self.probe(error)
        self.assertEqual(result['state'], 'empty')
        self.assertIn('tray is closed', result['message'])
        self.assertIn('cannot detect this disc', result['message'])
        self.assertNotIn('tray is open', result['message'])
        self.assertEqual((result['details']['sense_key'], result['details']['asc'], result['details']['ascq']), (2, 0x3a, 1))
        self.assert_numeric_details(result, error)

    def test_open_tray_is_distinct_from_closed_tray(self):
        error = diagnostic_fields(r.sense_error(fixed_sense(2, 0x3a, 2)))
        result = self.probe(error)
        self.assertEqual(result['state'], 'empty')
        self.assertIn('tray is open', result['message'])
        self.assertNotIn('tray is closed', result['message'])
        self.assertEqual(result['details']['ascq'], 2)
        self.assert_numeric_details(result, error)

    def test_descriptor_format_retains_identical_closed_tray_meaning(self):
        sense = bytes.fromhex('72023a0100000000')
        result = self.probe(r.sense_error(sense))
        self.assertEqual(result['state'], 'empty')
        self.assertIn('tray is closed', result['message'])
        self.assertEqual(result['details']['sense_hex'], sense.hex())
        self.assertEqual(result['details']['ascq'], 1)

    def test_disconnection_keeps_numeric_windows_error_and_closes_handle(self):
        error = diagnostic_fields(r.DriveUnavailable('DVD writer disconnected'))
        result = self.probe(error)
        self.assertEqual(result['state'], 'disconnected')
        self.assert_numeric_details(result, error)

    def test_generic_attention_keeps_failed_command_details(self):
        error = diagnostic_fields(r.sense_error(fixed_sense(5, 0x24, 0)))
        result = self.probe(error)
        self.assertEqual(result['state'], 'attention')
        self.assertEqual(result['details']['asc'], 0x24)
        self.assert_numeric_details(result, error)

    def test_constructor_disconnection_has_details_without_closing_uncreated_device(self):
        error = diagnostic_fields(r.DriveUnavailable('Cannot open writer'))
        with patch.object(r, 'Disc', side_effect=error) as constructor:
            result = r.probe_disc('F:')
        self.assertEqual(result['state'], 'disconnected')
        self.assert_numeric_details(result, error)
        constructor.return_value.close.assert_not_called()

    def test_unexpected_failure_propagates_after_closing_device(self):
        device = Mock()
        device.command.side_effect = RuntimeError('unexpected probe implementation failure')
        with patch.object(r, 'Disc', return_value=device):
            with self.assertRaisesRegex(RuntimeError, 'unexpected probe'):
                r.probe_disc('F:')
        device.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
