"""Device/MockDevice interface parity. The app is verified with --mock, so a
mock that drifts from the real class hides bugs until they reach hardware."""
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from siminput_updater.device import Device, DeviceLike  # noqa: E402
from siminput_updater.mock_device import MockDevice  # noqa: E402

PROTOCOL_METHODS = [
    name for name, member in vars(DeviceLike).items()
    if not name.startswith("_") and callable(member)
]


class Parity(unittest.TestCase):
    def test_both_implement_the_protocol_surface(self):
        for cls in (Device, MockDevice):
            for name in PROTOCOL_METHODS:
                self.assertTrue(hasattr(cls, name), f"{cls.__name__} missing {name}")

    def test_signatures_accept_the_same_calls(self):
        for name in PROTOCOL_METHODS:
            proto_sig = inspect.signature(getattr(DeviceLike, name))
            for cls in (Device, MockDevice):
                impl = getattr(cls, name)
                if isinstance(inspect.getattr_static(cls, name), (staticmethod, classmethod)):
                    continue
                impl_params = list(inspect.signature(impl).parameters)
                proto_params = list(proto_sig.parameters)
                self.assertEqual(
                    impl_params[:len(proto_params)], proto_params,
                    f"{cls.__name__}.{name} signature drifted from DeviceLike",
                )


if __name__ == "__main__":
    unittest.main()
