/** Sorting what someone picked into PDFs and photos. The server decides for
 *  real, from the bytes (backend app/ingest/upload.py); this only chooses
 *  between uploading at once (PDFs) and gathering pages (photos). */

/** Backend app/ingest/photos.py MAX_PHOTOS. */
export const MAX_PHOTOS = 10;

const PHOTO_EXTENSION = /\.(jpe?g|png|heic|heif|webp)$/i;

export function isPdf(file: Pick<File, "name" | "type">): boolean {
  return file.type === "application/pdf" || (!file.type && /\.pdf$/i.test(file.name));
}

/** Some browsers give HEIC no type at all, so the extension counts too. */
export function isPhoto(file: Pick<File, "name" | "type">): boolean {
  return file.type.startsWith("image/") || PHOTO_EXTENSION.test(file.name);
}

export function pageLabel(index: number): string {
  return `Page ${index + 1}`;
}
