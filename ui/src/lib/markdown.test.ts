import { describe, expect, it } from "vitest";
import { renderInline, renderMarkdown } from "./markdown";

describe("renderMarkdown", () => {
  it("renders headings, paragraphs and both list kinds", () => {
    const html = renderMarkdown(
      ["## What you need", "", "A sign-in.", "Two lines join.", "", "- one", "- two", "", "1. first", "2. second", "", "### Small"].join(
        "\n",
      ),
    );
    expect(html).toContain("<h2>What you need</h2>");
    expect(html).toContain("<p>A sign-in. Two lines join.</p>");
    expect(html).toContain("<ul><li>one</li><li>two</li></ul>");
    expect(html).toContain("<ol><li>first</li><li>second</li></ol>");
    expect(html).toContain("<h3>Small</h3>");
  });

  it("renders bold, code and links, and drops unsafe link targets", () => {
    expect(renderInline("**Save** the `KEY=value` line")).toBe(
      "<strong>Save</strong> the <code>KEY=value</code> line",
    );
    expect(renderMarkdown("See [Slack](#/getting-started/slack).")).toContain(
      '<a href="#/getting-started/slack">Slack</a>',
    );
    expect(renderMarkdown("Open [Slack](https://api.slack.com/apps).")).toContain(
      'href="https://api.slack.com/apps" rel="noopener" target="_blank"',
    );
    expect(renderMarkdown("Bad [link](javascript:steal).")).toBe("<p>Bad link.</p>");
  });

  it("escapes HTML in the source so nothing but its own tags reach the page", () => {
    const html = renderMarkdown("Type <b>bold</b> & \"quotes\".");
    expect(html).toBe("<p>Type &lt;b&gt;bold&lt;/b&gt; &amp; &quot;quotes&quot;.</p>");
  });

  it("joins a wrapped list item's indented continuation lines", () => {
    const html = renderMarkdown("- first line\n  continues here\n- second");
    expect(html).toBe("<ul><li>first line continues here</li><li>second</li></ul>");
  });
});
