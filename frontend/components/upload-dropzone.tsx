"use client";

import { ChevronDown, ChevronUp, File, UploadCloud, X } from "lucide-react";
import { useCallback, useState } from "react";
import { useDropzone } from "react-dropzone";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { cn, extensionOf, formatBytes } from "@/lib/utils";

/** Rows rendered before the user expands a long batch (fits without inner scrolling). */
const COLLAPSED_ROWS = 4;

interface UploadDropzoneProps {
  files: File[];
  onFilesChange: (files: File[]) => void;
  extensions: string[];
  maxFiles: number;
  maxSizeMb: number;
  disabled?: boolean;
}

export function UploadDropzone({
  files,
  onFilesChange,
  extensions,
  maxFiles,
  maxSizeMb,
  disabled,
}: UploadDropzoneProps) {
  const [showAll, setShowAll] = useState(false);
  const accept = extensions.join(", ");
  const maxBytes = maxSizeMb * 1024 * 1024;

  const onDrop = useCallback(
    (accepted: File[]) => {
      const invalid: string[] = [];
      const acceptedFiles: File[] = [];
      for (const file of accepted) {
        if (!extensions.includes(`.${extensionOf(file.name)}`)) {
          invalid.push(`${file.name}: format tidak didukung`);
          continue;
        }
        if (file.size > maxBytes) {
          invalid.push(`${file.name}: melebihi ${maxSizeMb} MB`);
          continue;
        }
        acceptedFiles.push(file);
      }
      if (invalid.length) {
        toast.error("Beberapa berkas tidak dapat ditambahkan", {
          description: invalid.slice(0, 3).join(" • "),
        });
      }
      const combined = [...files, ...acceptedFiles].slice(0, maxFiles);
      if (files.length + acceptedFiles.length > maxFiles) {
        toast.error(`Maksimal ${maxFiles} berkas untuk alat ini.`);
      }
      onFilesChange(combined);
    },
    [extensions, files, maxBytes, maxFiles, maxSizeMb, onFilesChange],
  );

  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({
    onDrop,
    multiple: maxFiles > 1,
    noClick: true,
    disabled,
    maxSize: maxBytes,
  });

  const removeFile = (index: number) => {
    onFilesChange(files.filter((_, fileIndex) => fileIndex !== index));
  };

  const hasFiles = files.length > 0;
  const collapsible = files.length > COLLAPSED_ROWS;
  const visibleFiles = showAll || !collapsible ? files : files.slice(0, COLLAPSED_ROWS);
  const hiddenCount = files.length - visibleFiles.length;

  return (
    <div className="space-y-2">
      <div
        {...getRootProps()}
        className={cn(
          "relative rounded-xl border-2 border-dashed border-border bg-muted/30 transition-colors",
          hasFiles
            ? "flex flex-wrap items-center justify-between gap-2 px-3 py-2"
            : "flex flex-col items-center justify-center px-6 py-6 text-center",
          isDragActive && "border-primary bg-primary/5",
          disabled && "cursor-not-allowed opacity-60",
        )}
      >
        <input {...getInputProps()} aria-label="Pilih berkas untuk diunggah" />
        {hasFiles ? (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <UploadCloud className="h-4 w-4 shrink-0 text-primary" aria-hidden />
            {isDragActive ? (
              "Lepaskan untuk menambah berkas"
            ) : (
              <>
                Tarik berkas lain atau{" "}
                <button
                  type="button"
                  onClick={open}
                  className="font-medium text-primary underline-offset-4 hover:underline"
                  disabled={disabled}
                >
                  pilih dari perangkat
                </button>
              </>
            )}
          </p>
        ) : (
          <>
            <span className="grid h-10 w-10 place-items-center rounded-full bg-primary/10 text-primary">
              <UploadCloud className="h-5 w-5" aria-hidden />
            </span>
            <p className="mt-3 text-sm font-medium">
              {isDragActive ? "Lepaskan berkas di sini" : "Tarik & lepas berkas di sini"}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              atau{" "}
              <button
                type="button"
                onClick={open}
                className="font-medium text-primary underline-offset-4 hover:underline"
                disabled={disabled}
              >
                pilih dari perangkat
              </button>
            </p>
          </>
        )}
        <span className="text-[11px] text-muted-foreground">
          {hasFiles ? `${files.length}/${maxFiles} berkas · ${maxSizeMb} MB/berkas` : `${accept} · maks. ${maxFiles} berkas · ${maxSizeMb} MB per berkas`}
        </span>
      </div>

      {hasFiles && (
        <ul
          className="max-h-[60vh] space-y-1.5 overflow-y-auto pr-1"
          aria-label="Berkas yang dipilih"
        >
          {visibleFiles.map((file, index) => (
            <li
              key={`${file.name}-${file.size}-${index}`}
              className="flex items-center gap-2.5 rounded-lg border bg-card px-2.5 py-1.5"
            >
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-primary/10 text-primary">
                <File className="h-3.5 w-3.5" aria-hidden />
              </span>
              <div className="min-w-0 flex-1 leading-tight">
                <p className="truncate text-xs font-medium">{file.name}</p>
                <p className="text-[11px] text-muted-foreground">
                  {formatBytes(file.size)} · {extensionOf(file.name).toUpperCase()}
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => removeFile(index)}
                aria-label={`Hapus ${file.name}`}
                disabled={disabled}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {collapsible && (
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          aria-expanded={showAll}
          className="flex w-full items-center justify-center gap-1 rounded-lg border border-dashed px-2 py-1.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
        >
          {showAll ? (
            <>
              <ChevronUp className="h-3.5 w-3.5" aria-hidden /> Ringkas daftar
            </>
          ) : (
            <>
              <ChevronDown className="h-3.5 w-3.5" aria-hidden /> Tampilkan {hiddenCount} berkas lainnya
            </>
          )}
        </button>
      )}
    </div>
  );
}
