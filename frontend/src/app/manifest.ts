import type { MetadataRoute } from "next";

/** What a phone needs to put the app on its home screen and open it like an
 *  app: full screen, straight to the invoices (where "Take photo" is). */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Invoice Intelligence",
    short_name: "Invoices",
    description: "Add invoices, catch price increases, and see what to ask your rep for.",
    start_url: "/invoices",
    scope: "/",
    display: "standalone",
    background_color: "#f8fafc",
    theme_color: "#4ade80",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
