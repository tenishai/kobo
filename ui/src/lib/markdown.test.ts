import { describe, expect, it } from "vitest";
import { renderInline, renderMarkdown } from "./markdown";

describe("renderMarkdown", () => {
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

});
