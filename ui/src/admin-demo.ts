// The standalone demo entry (admin-panel demo, 2026-10-12): the room's
// shell with the admin panel open, backed by the in-memory store, so the
// panel can be shown from one static page with no API behind it.
import { mount } from "svelte";
import "./app.css";
import AdminDemo from "./AdminDemo.svelte";

const target = document.getElementById("app");
if (!target) {
  throw new Error("missing #app mount element");
}

export default mount(AdminDemo, { target });
