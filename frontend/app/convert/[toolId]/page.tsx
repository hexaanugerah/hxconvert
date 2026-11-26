import type { Metadata } from "next";

import { Converter } from "@/components/converter";
import { PdfEditor } from "@/components/pdf-editor";

interface PageProps {
  params: Promise<{ toolId: string }>;
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { toolId } = await params;
  const label = toolId
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
  return { title: label };
}

export default async function ConvertToolPage({ params }: PageProps) {
  const { toolId } = await params;
  // Editing posts a structured element list instead of a plain option form, so
  // it has its own workspace rather than the generic converter UI.
  if (toolId === "edit-pdf") return <PdfEditor />;
  return <Converter toolId={toolId} />;
}
