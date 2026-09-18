"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

export default function UploadForm() {
  const router = useRouter();
  const [status, setStatus] = useState<"idle" | "uploading" | "error">("idle");

  async function handleChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;

    setStatus("uploading");
    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch(`${API_BASE}/invoices?tenant_id=${TENANT_ID}`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error(await res.text());
      setStatus("idle");
      router.refresh();
    } catch {
      setStatus("error");
    } finally {
      e.target.value = "";
    }
  }

  return (
    <div className="mb-4 flex items-center gap-3">
      <label className="cursor-pointer rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50">
        Upload invoice PDF
        <input type="file" accept="application/pdf" className="hidden" onChange={handleChange} />
      </label>
      {status === "uploading" && <span className="text-sm text-gray-500">Uploading…</span>}
      {status === "error" && <span className="text-sm text-red-600">Upload failed.</span>}
    </div>
  );
}
