import { useState, useRef, useCallback, useEffect } from "react";
import "./App.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const API = `${API_BASE}/api/chapter`;

// ── localStorage helpers ──
const STORAGE_KEY = "novel-reader-state";

function saveState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch { /* quota exceeded or private browsing */ }
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function clearState() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch { /* ignore */ }
}

function App() {
  // Restore from localStorage on mount
  const saved = useRef(loadState());

  const [url, setUrl] = useState(saved.current?.url || "");
  const [chapterText, setChapterText] = useState(saved.current?.chapterText || "");
  const [nextUrl, setNextUrl] = useState(saved.current?.nextUrl || null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  // History stack for "Previous" button
  const [history, setHistory] = useState(saved.current?.history || []);

  // Prefetch cache
  const prefetchRef = useRef({ text: null, nextUrl: null, forUrl: null });

  // Persist state to localStorage whenever it changes
  useEffect(() => {
    if (chapterText) {
      saveState({ url, chapterText, nextUrl, history });
    }
  }, [url, chapterText, nextUrl, history]);

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
      // Push current state to history if we already have content
      if (chapterText && url) {
        setHistory((prev) => [...prev, { url, chapterText, nextUrl }]);
      }
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

    // Save current to history
    if (chapterText && url) {
      setHistory((prev) => [...prev, { url, chapterText, nextUrl }]);
    }

    // Use prefetched data if available
    if (prefetchRef.current.forUrl === nextUrl && prefetchRef.current.text) {
      const cached = prefetchRef.current;
      setChapterText(cached.text);
      setUrl(nextUrl);
      setNextUrl(cached.nextUrl);
      prefetchRef.current = { text: null, nextUrl: null, forUrl: null };
      prefetch(cached.nextUrl);
      window.scrollTo({ top: 0, behavior: "smooth" });
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
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handlePrevious = () => {
    if (history.length === 0) return;
    const prev = history[history.length - 1];
    setHistory((h) => h.slice(0, -1));
    setChapterText(prev.chapterText);
    setUrl(prev.url);
    setNextUrl(prev.nextUrl);
    setCopied(false);
    setError("");
    prefetchRef.current = { text: null, nextUrl: null, forUrl: null };
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const handleClear = () => {
    setUrl("");
    setChapterText("");
    setNextUrl(null);
    setHistory([]);
    setError("");
    setCopied(false);
    prefetchRef.current = { text: null, nextUrl: null, forUrl: null };
    clearState();
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
      <div className="header-row">
        <h1 className="title">📖 Novel Reader</h1>
        {chapterText && (
          <button className="btn clear-btn" onClick={handleClear}>
            ✕ Clear
          </button>
        )}
      </div>

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
            <button
              className="btn prev-btn"
              onClick={handlePrevious}
              disabled={history.length === 0 || loading}
            >
              ← Previous
            </button>
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
