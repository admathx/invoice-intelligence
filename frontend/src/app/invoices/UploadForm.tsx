"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { useHydrated } from "@/lib/useHydrated";

import { MAX_PHOTOS, isPdf, isPhoto, pageLabel } from "./uploadFiles";

type Page = { file: File; preview: string };

/** Getting invoices in: a PDF (or several, one invoice each), or photos of a
 *  paper invoice.
 *
 * Photos are gathered first, a page at a time (on a phone, "Take photo" opens
 * the camera), and sent together as one invoice; the server turns them into a
 * PDF (backend app/ingest/photos.py). */
export default function UploadForm() {
  const router = useRouter();
  const locationId = useLocationId();
  const hydrated = useHydrated();
  const [pages, setPages] = useState<Page[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Previews are object URLs; each is released when its page goes, and all
  // of them when the form does.
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  useEffect(() => () => pagesRef.current.forEach((p) => URL.revokeObjectURL(p.preview)), []);

  async function send(files: File[]): Promise<string | null> {
    const formData = new FormData();
    for (const file of files) formData.append("file", file);
    try {
      const res = await api(`/invoices?tenant_id=${locationId}`, { method: "POST", body: formData });
      if (res.ok) return null;
      const body = await res.json().catch(() => null);
      return formatApiError(body?.detail, "Upload failed.");
    } catch {
      return "Couldn't reach the server. Nothing was uploaded.";
    }
  }

  async function uploadPdfs(pdfs: File[]) {
    setBusy(true);
    const failures: string[] = [];
    // One at a time, each its own invoice, so one bad file doesn't sink the rest.
    for (const pdf of pdfs) {
      const problem = await send([pdf]);
      if (problem) failures.push(pdfs.length > 1 ? `${pdf.name}: ${problem}` : problem);
    }
    setBusy(false);
    const uploaded = pdfs.length - failures.length;
    if (failures.length) setError(failures.join(" "));
    if (uploaded) {
      setNotice(`Uploaded ${uploaded} invoice${uploaded === 1 ? "" : "s"}. Reading ${uploaded === 1 ? "it" : "them"} now.`);
      router.refresh();
    }
  }

  function addPhotos(photos: File[]) {
    const room = MAX_PHOTOS - pages.length;
    if (photos.length > room) {
      setError(`One invoice can have at most ${MAX_PHOTOS} photos.`);
      photos = photos.slice(0, room);
    }
    setPages((current) => [...current, ...photos.map((file) => ({ file, preview: URL.createObjectURL(file) }))]);
  }

  function handleChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ""; // choosing the same file again still fires
    if (!files.length) return;
    setError(null);
    setNotice(null);
    if (files.every(isPdf) && pages.length === 0) {
      void uploadPdfs(files);
    } else if (files.every(isPhoto)) {
      addPhotos(files);
    } else if (pages.length > 0) {
      setError("Add photos of this invoice's pages, or upload these photos first.");
    } else {
      setError("Choose PDFs or photos, not both.");
    }
  }

  function removePage(index: number) {
    setPages((current) => {
      URL.revokeObjectURL(current[index].preview);
      return current.filter((_, i) => i !== index);
    });
  }

  function clearPages() {
    pages.forEach((p) => URL.revokeObjectURL(p.preview));
    setPages([]);
  }

  async function uploadPhotos() {
    setBusy(true);
    setError(null);
    const problem = await send(pages.map((p) => p.file));
    setBusy(false);
    if (problem) {
      setError(problem);
      return;
    }
    clearPages();
    setNotice("Uploaded. Reading the invoice now.");
    router.refresh();
  }

  const disabled = !hydrated || busy;
  const full = pages.length >= MAX_PHOTOS;
  // A label is the button (a hidden input inside it). Styled disabled by
  // hand, since a label has no disabled state of its own.
  const off = "pointer-events-none opacity-50";

  return (
    <>
      {/* While photos are being gathered, their tray has the buttons. */}
      {pages.length === 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <CameraButton label="Take photo" disabled={disabled} onChange={handleChosen} />
          <label className={`btn-primary cursor-pointer ${disabled ? off : ""}`}>
            <span aria-hidden>↑</span> {busy ? "Uploading…" : "Upload invoice"}
            <input
              type="file"
              accept="application/pdf,image/*,.heic,.heif"
              multiple
              className="hidden"
              disabled={disabled}
              onChange={handleChosen}
              data-testid="upload-input"
            />
          </label>
        </div>
      )}

      {(error || notice) && pages.length === 0 && (
        <p
          role={error ? "alert" : "status"}
          className={`order-last w-full text-sm ${error ? "text-red-600" : "text-brand-700"}`}
        >
          {error ?? notice}
        </p>
      )}

      {pages.length > 0 && (
        <section
          aria-label="Photos of one invoice"
          className="card order-last w-full border-l-4 border-l-brand-400 p-4"
          data-testid="photo-tray"
        >
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 className="section-title">
              Photos of one invoice{" "}
              <span className="badge bg-brand-100 text-brand-800">
                {pages.length} page{pages.length === 1 ? "" : "s"}
              </span>
            </h2>
            <p className="text-xs text-gray-500">In page order. Up to {MAX_PHOTOS} photos.</p>
          </div>

          <ol className="mt-3 flex flex-wrap gap-3">
            {pages.map((page, i) => (
              <li key={page.preview} className="relative w-24">
                <Thumbnail page={page} />
                <div className="mt-1 flex items-center justify-between text-xs text-gray-600">
                  <span>{pageLabel(i)}</span>
                  <button
                    type="button"
                    onClick={() => removePage(i)}
                    disabled={busy}
                    className="font-semibold text-red-700 hover:underline disabled:opacity-50"
                    aria-label={`Remove ${pageLabel(i).toLowerCase()}`}
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ol>

          {error && (
            <p role="alert" className="mt-3 text-sm text-red-600">
              {error}
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button type="button" className="btn-primary" disabled={disabled} onClick={() => void uploadPhotos()}>
              <span aria-hidden>↑</span>{" "}
              {busy ? "Uploading…" : `Upload invoice (${pages.length} page${pages.length === 1 ? "" : "s"})`}
            </button>
            <CameraButton label="Take next page" disabled={disabled || full} onChange={handleChosen} />
            <label className={`btn-secondary cursor-pointer ${disabled || full ? off : ""}`}>
              + Add page
              <input
                type="file"
                accept="image/*,.heic,.heif"
                multiple
                className="hidden"
                disabled={disabled || full}
                onChange={handleChosen}
                data-testid="add-page-input"
              />
            </label>
            <button type="button" className="btn-secondary" disabled={busy} onClick={clearPages}>
              Cancel
            </button>
          </div>
        </section>
      )}
    </>
  );
}

/** A phone's own camera, straight away. Phones only: desktop browsers ignore
 *  `capture`, and there "Upload invoice" and "Add page" do the same job. */
function CameraButton({
  label,
  disabled,
  onChange,
}: {
  label: string;
  disabled: boolean;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <label className={`btn-secondary cursor-pointer sm:hidden ${disabled ? "pointer-events-none opacity-50" : ""}`}>
      <span aria-hidden>📷</span> {label}
      <input
        type="file"
        accept="image/*"
        capture="environment"
        className="hidden"
        disabled={disabled}
        onChange={onChange}
        data-testid="camera-input"
      />
    </label>
  );
}

function Thumbnail({ page }: { page: Page }) {
  // Browsers other than Safari can't show HEIC; the page is still fine to
  // send, the server reads it.
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div className="grid h-32 w-24 place-items-center overflow-hidden rounded-md border border-gray-200 bg-gray-50 p-1 text-center text-[11px] text-gray-500 [overflow-wrap:anywhere]">
        {page.file.name}
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- a local object URL, nothing to optimize
    <img
      src={page.preview}
      alt=""
      onError={() => setBroken(true)}
      className="h-32 w-24 rounded-md border border-gray-200 bg-gray-50 object-cover"
    />
  );
}
