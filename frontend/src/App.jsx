import { useState, useRef, useCallback } from "react";
import "./App.css";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";
const API = `${API_BASE}/api/chapter`;

function App() {
  const [url, setUrl] = useState("");
  const [chapterText, setChapterText] = useState("");
  const [nextUrl, setNextUrl] = useState(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  // Prefetch cache
  const prefetchRef = useRef({ text: null, nextUrl: null, forUrl: null });

  const fetchChapter = useCallback(async (chapterUrl) => {
    const res = await fetch(`${API}?url=${encodeURIComponent(chapterUrl)}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Error ${res.status}`);
    }
    return res.json();
  }, []);

  const prefetch = useCallback(
    async (nextChapterUrl) => {
      if (!nextChapterUrl) return;
      try {
        const data = await fetchChapter(nextChapterUrl);
        prefetchRef.current = {
          text: data.text,
          nextUrl: data.next_url,
          forUrl: nextChapterUrl,
        };
      } catch {
        prefetchRef.current = { text: null, nextUrl: null, forUrl: null };
      }
    },
    [fetchChapter]
  );

  const handleFetch = async () => {
    if (!url.trim()) return;
    setLoading(true);
    setError("");
    setCopied(false);
    try {
      const data = await fetchChapter(url.trim());
      setChapterText(data.text);
      setNextUrl(data.next_url);
      prefetch(data.next_url);
    } catch (e) {
      setError(e.message);
      setChapterText("");
      setNextUrl(null);
    } finally {
      setLoading(false);
    }
  };

  const handleNext = async () => {
    if (!nextUrl) return;
    setCopied(false);
    setError("");

    // Use prefetched data if available
    if (prefetchRef.current.forUrl === nextUrl && prefetchRef.current.text) {
      const cached = prefetchRef.current;
      setChapterText(cached.text);
      setUrl(nextUrl);
      setNextUrl(cached.nextUrl);
      prefetchRef.current = { text: null, nextUrl: null, forUrl: null };
      prefetch(cached.nextUrl);
      return;
    }

    // Fallback: fetch normally
    setLoading(true);
    try {
      const data = await fetchChapter(nextUrl);
      setChapterText(data.text);
      setUrl(nextUrl);
      setNextUrl(data.next_url);
      prefetch(data.next_url);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(chapterText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Failed to copy");
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter") handleFetch();
  };

  return (
    <div className="app">
      <h1 className="title">📖 Novel Reader</h1>

      <div className="input-row">
        <input
          type="text"
          className="url-input"
          placeholder="Paste chapter URL here…"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button className="btn fetch-btn" onClick={handleFetch} disabled={loading || !url.trim()}>
          {loading ? "Loading…" : "Fetch"}
        </button>
      </div>

      {error && <p className="error">{error}</p>}

      {chapterText && (
        <>
          <div className="chapter-box">{chapterText}</div>

          <div className="action-row">
            <button className="btn copy-btn" onClick={handleCopy}>
              {copied ? "✓ Copied!" : "📋 Copy"}
            </button>
            <button
              className="btn next-btn"
              onClick={handleNext}
              disabled={!nextUrl || loading}
            >
              Next Chapter →
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export default App;
