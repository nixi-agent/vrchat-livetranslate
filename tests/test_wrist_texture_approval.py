"""D3D uploads and rebinds retain approval and generation ownership; fake APIs only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vlt.output.openvr_overlay import WristOverlay
from vlt.output.overlay import OverlayConfig
from vlt.session.chatgpt_output import scope


class Approval:
    valid = True

    def __call__(self, direction=None):
        return self.valid


class TextureApprovalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('vlt.output.openvr_overlay._make_texture_t', return_value=object()))
        self.enterContext(patch('vlt.output.openvr_overlay.D3D11Texture',
                                side_effect=AssertionError('real GPU opened')))
        self.out = WristOverlay(OverlayConfig())
        self.out.available, self.out._handle = True, 1
        self.out._overlay = Mock()
        self.out._d3d = Mock(shared_handle=1)
        self.out._d3d.device_removed_reason.return_value = 0
        self.approval = Approval()
        self.image = Image.new('RGBA', (4, 4))

    def test_revoked_input_never_copied_or_submitted(self):
        self.approval.valid = False
        with scope(self.approval):
            self.out._upload_texture(self.image)
        self.out._d3d.upload.assert_not_called()
        self.out._overlay.setOverlayTexture.assert_not_called()

    def test_revoke_during_pixel_conversion_prevents_gpu_copy(self):
        image = Mock(mode='RGBA', size=(4, 4))
        def pixels():
            self.approval.valid = False
            return bytes(64)
        image.tobytes.side_effect = pixels
        with scope(self.approval):
            self.out._upload_texture(image)
        self.out._d3d.upload.assert_not_called()
        self.out._overlay.setOverlayTexture.assert_not_called()

    def test_revoke_during_copy_prevents_submit_and_hides_texture(self):
        self.out._d3d.upload.side_effect = lambda _: setattr(self.approval, 'valid', False)
        with scope(self.approval):
            self.out._upload_texture(self.image)
        self.out._overlay.setOverlayTexture.assert_not_called()
        self.out._overlay.setOverlayAlpha.assert_called_with(1, 0.0)

    def test_close_during_copy_never_records_old_upload_success(self):
        self.out._d3d.upload.side_effect = lambda _: self.out.close()
        with scope(self.approval):
            self.out._upload_texture(self.image)
        self.assertEqual(self.out.frames_updated, 0)
        self.out._overlay.setOverlayTexture.assert_not_called()

    def test_revoked_existing_texture_never_rebound(self):
        self.out._uploaded_approval = self.approval
        self.approval.valid = False
        self.out._set_overlay_texture()
        self.out._overlay.setOverlayTexture.assert_not_called()

    def test_success_keeps_the_original_approval(self):
        with scope(self.approval):
            self.out._upload_texture(self.image)
        self.assertIs(self.out._uploaded_approval, self.approval)
        self.assertEqual(self.out.frames_updated, 1)
        self.out._overlay.setOverlayTexture.assert_called_once()

    def test_revoke_during_texture_descriptor_prevents_rebind(self):
        self.out._uploaded_approval = self.approval
        def descriptor(_):
            self.approval.valid = False
            return object()
        with patch('vlt.output.openvr_overlay._make_texture_t', side_effect=descriptor):
            self.out._set_overlay_texture()
        self.out._overlay.setOverlayTexture.assert_not_called()

    def test_close_during_texture_descriptor_prevents_rebind(self):
        def descriptor(_):
            self.out.close()
            return object()
        with patch('vlt.output.openvr_overlay._make_texture_t', side_effect=descriptor):
            self.out._set_overlay_texture()
        self.out._overlay.setOverlayTexture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
