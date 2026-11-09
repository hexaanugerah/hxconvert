import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { FastModeNotice } from "./fast-mode-notice";

describe("FastModeNotice", () => {
  it("renders nothing unless fast mode is selected", () => {
    expect(renderToStaticMarkup(<FastModeNotice visible={false} />)).toBe("");
    expect(renderToStaticMarkup(<FastModeNotice visible={false} dpi={110} />)).toBe("");
  });

  it("warns that fast mode output is images, not editable text", () => {
    const html = renderToStaticMarkup(<FastModeNotice visible />);

    expect(html).toContain("Mode cepat menghasilkan gambar, bukan teks");
    expect(html).toContain("tidak dapat diedit");
    expect(html).toContain("72 DPI");
  });

  it("shows the fast_dpi value currently selected", () => {
    const html = renderToStaticMarkup(<FastModeNotice visible dpi={110} />);

    expect(html).toContain("110 DPI");
    expect(html).not.toContain("72 DPI");
  });
});
