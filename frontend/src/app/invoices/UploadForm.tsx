"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";

export default function UploadForm() {
  const router = useRouter();
  const locationId = useLocationId();
  const [status, setStatus] = useState<"idle" | "uploading" | "error">("idle");
  // The API says why a file was refused (not a PDF, too large); the form used
  // to discard that and show "Upload failed." for everything.
  const [message, setMessage] = useState("Upload failed.");

  async function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;

    setStatus("uploading");
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await api(`/invoices?tenant_id=${locationId}`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setMessage(formatApiError(body?.detail, "Upload failed."));
        setStatus("error");
        return;
      }
      setStatus("idle");
      router.refresh();
    } catch {
      setMessage("Couldn't reach the server. Nothing was uploaded.");
      setStatus("error");
    } finally {
      e.target.value = "";
    }
  }

  return (
    <div className="flex items-center gap-3">
      {status === "error" && <span className="text-sm text-red-600">{message}</span>}
      <label className={`btn-primary cursor-pointer ${status === "uploading" ? "pointer-events-none opacity-60" : ""}`}>
        <span aria-hidden>↑</span> {status === "uploading" ? "Uploading…" : "Upload invoice PDF"}
        <input type="file" accept="application/pdf" className="hidden" onChange={handleChange} />
      </label>
    </div>
  );
}
