// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { UploadDropzone } from "./upload-dropzone";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function pdfFiles(count: number): File[] {
  return Array.from(
    { length: count },
    (_, index) => new File(["x"], `doc-${index + 1}.pdf`, { type: "application/pdf" }),
  );
}

function render(count: number): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <UploadDropzone
        files={pdfFiles(count)}
        onFilesChange={() => {}}
        extensions={[".pdf"]}
        maxFiles={50}
        maxSizeMb={200}
      />,
    );
  });
  return container;
}

function rowCount(el: HTMLElement): number {
  return el.querySelectorAll('ul[aria-label="Berkas yang dipilih"] > li').length;
}

function toggleButton(el: HTMLElement): HTMLButtonElement | undefined {
  return [...el.querySelectorAll("button")].find((button) =>
    /berkas lainnya|Ringkas daftar/.test(button.textContent ?? ""),
  ) as HTMLButtonElement | undefined;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("UploadDropzone file list", () => {
  it("caps the rows and counts the hidden ones", () => {
    const el = render(10);

    expect(rowCount(el)).toBe(4);
    expect(toggleButton(el)?.textContent).toContain("Tampilkan 6 berkas lainnya");
    expect(toggleButton(el)?.getAttribute("aria-expanded")).toBe("false");
  });

  it("shows every row after expanding and collapses again", () => {
    const el = render(10);

    act(() => toggleButton(el)?.click());
    expect(rowCount(el)).toBe(10);
    expect(toggleButton(el)?.textContent).toContain("Ringkas daftar");
    expect(toggleButton(el)?.getAttribute("aria-expanded")).toBe("true");

    act(() => toggleButton(el)?.click());
    expect(rowCount(el)).toBe(4);
    expect(toggleButton(el)?.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders no toggle when every file already fits", () => {
    const el = render(4);

    expect(rowCount(el)).toBe(4);
    expect(toggleButton(el)).toBeUndefined();
  });
});
