"""GDI surfaces: pixel format correctness.

UpdateLayeredWindow needs premultiplied BGRA. Getting that wrong shows up as
dark fringes or washed-out edges rather than an exception, so it is worth
pinning down exactly.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from fun_tab.gdi import Dib


@pytest.fixture
def dib():
    surface = Dib()
    if not surface.ensure(4, 3):
        pytest.skip("no GDI device context available")
    yield surface
    surface.destroy()


def test_ensure_allocates_a_top_down_view(dib):
    assert dib.size == (4, 3)
    assert dib.view.shape == (3, 4, 4), "(height, width, BGRA)"
    assert dib.hdc


def test_ensure_is_idempotent_for_the_same_size(dib):
    before = dib.hdc
    assert dib.ensure(4, 3) is True
    assert dib.hdc == before, "resizing to the same size should not reallocate"


def test_ensure_reallocates_on_resize(dib):
    assert dib.ensure(8, 5) is True
    assert dib.size == (8, 5)
    assert dib.view.shape == (5, 8, 4)


def test_write_swaps_to_bgra(dib):
    img = Image.new("RGBA", (4, 3), (10, 20, 30, 255))
    assert dib.write(img) is True
    blue, green, red, alpha = dib.view[0, 0]
    assert (red, green, blue, alpha) == (10, 20, 30, 255)


def test_write_premultiplies_alpha(dib):
    img = Image.new("RGBA", (4, 3), (200, 100, 50, 128))
    dib.write(img)
    blue, green, red, alpha = (int(v) for v in dib.view[1, 2])
    assert alpha == 128
    # Premultiplied: each channel scaled by alpha/255 (rounding is Pillow's).
    assert red == pytest.approx(200 * 128 / 255, abs=1)
    assert green == pytest.approx(100 * 128 / 255, abs=1)
    assert blue == pytest.approx(50 * 128 / 255, abs=1)


def test_fully_transparent_pixels_become_zero(dib):
    dib.write(Image.new("RGBA", (4, 3), (255, 255, 255, 0)))
    assert not dib.view.any(), "a transparent white must not leave white behind"


def test_opaque_mode_skips_the_premultiply(dib):
    img = Image.new("RGBA", (4, 3), (200, 100, 50, 128))
    dib.write(img, opaque=True)
    blue, green, red, _ = (int(v) for v in dib.view[0, 0])
    assert (red, green, blue) == (200, 100, 50)


def test_write_preserves_pixel_positions(dib):
    img = Image.new("RGBA", (4, 3), (0, 0, 0, 255))
    img.putpixel((3, 0), (255, 0, 0, 255))  # top-right
    dib.write(img)
    assert tuple(dib.view[0, 3]) == (0, 0, 255, 255), "top-right stays top-right"
    assert tuple(dib.view[2, 0]) == (0, 0, 0, 255)


def test_write_converts_other_modes(dib):
    assert dib.write(Image.new("RGB", (4, 3), (10, 20, 30))) is True
    assert tuple(dib.view[0, 0]) == (30, 20, 10, 255)


def test_write_rejects_a_size_mismatch(dib):
    assert dib.write(Image.new("RGBA", (9, 9))) is False


def test_write_before_ensure_is_refused():
    assert Dib().write(Image.new("RGBA", (4, 3))) is False


def test_write_box_leaves_the_rest_of_the_surface_standing(dib):
    dib.write(Image.new("RGBA", (4, 3), (0, 0, 0, 255)))
    assert dib.write_box(Image.new("RGBA", (2, 1), (255, 0, 0, 255)), 1, 1) is True

    assert tuple(dib.view[1, 1]) == (0, 0, 255, 255), "patch landed"
    assert tuple(dib.view[1, 2]) == (0, 0, 255, 255)
    assert tuple(dib.view[1, 0]) == (0, 0, 0, 255), "left of the patch untouched"
    assert tuple(dib.view[0, 1]) == (0, 0, 0, 255), "above the patch untouched"
    assert tuple(dib.view[2, 1]) == (0, 0, 0, 255), "below the patch untouched"


def test_write_box_premultiplies_like_a_full_write(dib):
    patch = Image.new("RGBA", (2, 2), (200, 100, 50, 128))
    dib.write_box(patch, 0, 0)
    from_box = tuple(int(v) for v in dib.view[0, 0])
    dib.write(Image.new("RGBA", (4, 3), (200, 100, 50, 128)))
    assert from_box == tuple(int(v) for v in dib.view[0, 0])


def test_write_box_refuses_to_spill_over_the_edge(dib):
    patch = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    assert dib.write_box(patch, 3, 0) is False, "would overrun the right edge"
    assert dib.write_box(patch, 0, 2) is False, "would overrun the bottom edge"
    assert dib.write_box(patch, -1, 0) is False
    assert not dib.view.any(), "a rejected patch must not write anything"


def test_write_box_before_ensure_is_refused():
    assert Dib().write_box(Image.new("RGBA", (2, 2)), 0, 0) is False


def test_fill_opaque_black(dib):
    dib.write(Image.new("RGBA", (4, 3), (255, 255, 255, 255)))
    dib.fill_opaque_black()
    assert np.array_equal(dib.view[..., :3], np.zeros((3, 4, 3), np.uint8))
    assert (dib.view[..., 3] == 255).all()


def test_destroy_is_safe_to_repeat(dib):
    dib.destroy()
    dib.destroy()
    assert dib.size == (0, 0)
    assert dib.view is None
