import type { Metadata } from "next";

import { VisualComparison } from "@/components/visual-comparison";

interface PageProps {
  params: Promise<{ jobId: string }>;
}

export const metadata: Metadata = {
  title: "Banding visual",
};

export default async function ComparePage({ params }: PageProps) {
  const { jobId } = await params;
  return <VisualComparison jobId={jobId} />;
}
