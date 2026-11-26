import type { Metadata } from "next";

import { WordEditor } from "@/components/word-editor";

export const metadata: Metadata = {
  title: "Edit Word",
  description: "Edit isi dokumen Word DOCX langsung di HXconvertX.",
};

export default function EditWordPage() {
  return <WordEditor />;
}
