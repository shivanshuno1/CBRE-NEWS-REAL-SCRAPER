import React, { useState, useMemo, useRef, useEffect } from 'react';
import * as XLSX from 'xlsx';

const API_BASE = 'http://localhost:8000';

export default function App() {
  // Config state
  const [file, setFile] = useState(null);
  const [nameCol, setNameCol] = useState('Account_name');
  const [keywords, setKeywords] = useState('opens, launches');
  const [days, setDays] = useState(120);
  const [headless, setHeadless] = useState(true);
  const [location, setLocation] = useState('India');

  // Job lifecycle state
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState(null);
  const [progress, setProgress] = useState({ completed_accounts: 0, total_accounts: 0 });
  const [results, setResults] = useState([]);
  const [error, setError] = useState('');
  const pollRef = useRef(null);

  const isLoading = jobStatus === 'running';

  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('ALL');

  const handleFileChange = (e) => {
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
    }
  };

  useEffect(() => {
    if (!jobId) return;

    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/scrape/status/${jobId}`);
        if (!res.ok) return;
        const data = await res.json();
        setJobStatus(data.status);
        setProgress({
          completed_accounts: data.completed_accounts,
          total_accounts: data.total_accounts,
        });
        setResults(data.results || []);

        if (data.status === 'error') {
          setError(data.error || 'The scraper hit an error.');
        }

        if (data.status !== 'running' && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch (err) {
        // ignore
      }
    };

    poll();
    pollRef.current = setInterval(poll, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [jobId]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) {
      setError('Please upload an Excel file containing your account names.');
      return;
    }

    setError('');
    setResults([]);
    setJobId(null);
    setJobStatus(null);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('name_col', nameCol);
    formData.append('keywords_str', keywords);
    formData.append('days', days);
    formData.append('headless', headless ? 'true' : 'false'); // ← FIXED
    formData.append('location', location);

    try {
      const response = await fetch(`${API_BASE}/api/scrape/start`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const errData = await response.json();
        throw new Error(errData.detail || 'Scraping request failed.');
      }

      const data = await response.json();
      setJobId(data.job_id);
      setJobStatus('running');
    } catch (err) {
      setError(err.message || 'An unexpected error occurred while starting the scraper.');
    }
  };

  const handleStop = async () => {
    if (!jobId) return;
    try {
      await fetch(`${API_BASE}/api/scrape/stop/${jobId}`, { method: 'POST' });
    } catch (err) {
      setError('Could not reach the server to stop the job, but your last checkpoint is still saved.');
    }
  };

  const filteredResults = useMemo(() => {
    return results.filter((row) => {
      const matchesStatus = statusFilter === 'ALL' || row.Status === statusFilter;
      const searchLower = searchQuery.toLowerCase();
      const matchesSearch =
        !searchQuery ||
        (row['Account Name'] && row['Account Name'].toLowerCase().includes(searchLower)) ||
        (row['Extracted Name'] && row['Extracted Name'].toLowerCase().includes(searchLower)) ||
        (row['Article Title'] && row['Article Title'].toLowerCase().includes(searchLower));

      return matchesStatus && matchesSearch;
    });
  }, [results, searchQuery, statusFilter]);

  const exportToExcel = () => {
    if (!results.length) return;
    const worksheet = XLSX.utils.json_to_sheet(results);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, 'Scraper Results');
    XLSX.writeFile(workbook, 'scraped_executive_news.xlsx');
  };

  const statusLabel = {
    running: 'Running',
    stopped: 'Stopped (checkpoint saved)',
    completed: 'Completed',
    error: 'Error',
  }[jobStatus] || '';

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <h1 style={styles.title}>Executive News & Growth Scraper</h1>
        <p style={styles.subtitle}>
          Extract leadership changes & expansion intelligence directly from Google News.
        </p>
      </header>

      <div style={styles.layout}>
        {/* Left Panel */}
        <div style={styles.card}>
          <h2 style={styles.cardTitle}>Scraper Configuration</h2>
          <form onSubmit={handleSubmit} style={styles.form}>
            <div style={styles.field}>
              <label style={styles.label}>Accounts File (.xlsx)</label>
              <input
                type="file"
                accept=".xlsx, .xls"
                onChange={handleFileChange}
                disabled={isLoading}
                style={styles.fileInput}
              />
              {file && <span style={styles.fileHint}>Selected: {file.name}</span>}
            </div>

            <div style={styles.field}>
              <label style={styles.label}>Account Name Column Header</label>
              <input
                type="text"
                value={nameCol}
                onChange={(e) => setNameCol(e.target.value)}
                disabled={isLoading}
                placeholder="e.g. Account_name"
                style={styles.input}
              />
            </div>

            <div style={styles.field}>
              <label style={styles.label}>Growth Keywords (comma-separated)</label>
              <input
                type="text"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                disabled={isLoading}
                placeholder="opens, launches, expands"
                style={styles.input}
              />
            </div>

            <div style={styles.field}>
              <label style={styles.label}>Geographic Location</label>
              <input
                type="text"
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                disabled={isLoading}
                placeholder="e.g. India, United States, UK"
                style={styles.input}
              />
              <span style={styles.fileHint}>
                Focuses search results to news from this country/region.
              </span>
            </div>

            <div style={styles.field}>
              <label style={styles.label}>Lookback Window (Days)</label>
              <input
                type="number"
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                disabled={isLoading}
                style={styles.input}
              />
            </div>

            <div style={styles.checkboxField}>
              <input
                type="checkbox"
                id="headless"
                checked={headless}
                onChange={(e) => setHeadless(e.target.checked)}
                disabled={isLoading}
              />
              <label htmlFor="headless" style={styles.checkboxLabel}>
                Run Chrome in Headless Mode
              </label>
            </div>

            {!isLoading && (
              <button type="submit" disabled={!file} style={{
                ...styles.button,
                ...(!file ? styles.buttonDisabled : {}),
              }}>
                Start Extraction
              </button>
            )}

            {isLoading && (
              <button type="button" onClick={handleStop} style={styles.stopButton}>
                Stop & Save Checkpoint
              </button>
            )}
          </form>

          {error && <div style={styles.errorBox}>{error}</div>}

          {jobId && (
            <div style={styles.checkpointBox}>
              <div style={{ fontWeight: 600, marginBottom: '0.4rem' }}>
                {statusLabel} — {progress.completed_accounts}/{progress.total_accounts} accounts
              </div>
              <a
                href={`${API_BASE}/api/scrape/download/${jobId}`}
                style={styles.checkpointLink}
              >
                Download checkpoint file (.xlsx)
              </a>
              <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: '0.3rem' }}>
                Updates after every account finishes — safe to download any time, even mid-run.
              </div>
            </div>
          )}
        </div>

        {/* Right Panel */}
        <div style={styles.mainContent}>
          {isLoading && results.length === 0 && (
            <div style={styles.loadingCard}>
              <div style={styles.spinner}></div>
              <h3>Extraction in Progress</h3>
              <p style={{ color: '#666', fontSize: '0.9rem' }}>
                Selenium is navigating Google News and analyzing article text. Results
                will start appearing here as each account finishes. You can stop and
                save a checkpoint at any point using the button on the left.
              </p>
            </div>
          )}

          {!isLoading && !jobId && results.length === 0 && !error && (
            <div style={styles.emptyCard}>
              <h3>No Results Yet</h3>
              <p style={{ color: '#666' }}>
                Upload your accounts sheet and click "Start Extraction" to view relevant executive news.
              </p>
            </div>
          )}

          {results.length > 0 && (
            <div style={styles.card}>
              <div style={styles.resultsHeader}>
                <div>
                  <h2 style={styles.cardTitle}>Extracted Records ({filteredResults.length})</h2>
                  <p style={{ color: '#666', fontSize: '0.85rem', margin: 0 }}>
                    Total entries so far: {results.length}
                    {isLoading ? ' (still running...)' : ''}
                  </p>
                </div>
                <button onClick={exportToExcel} style={styles.exportButton}>
                  Export to Excel
                </button>
              </div>

              <div style={styles.filterBar}>
                <input
                  type="text"
                  placeholder="Filter by account, name, or article title..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  style={{ ...styles.input, flex: 1 }}
                />
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value)}
                  style={{ ...styles.input, width: '180px' }}
                >
                  <option value="ALL">All Statuses</option>
                  <option value="OK">OK (Name Found)</option>
                  <option value="NO_NAME_FOUND">NO_NAME_FOUND</option>
                  <option value="NO_RELEVANT_NEWS">NO_RELEVANT_NEWS</option>
                  <option value="FETCH_FAILED">FETCH_FAILED</option>
                </select>
              </div>

              <div style={styles.tableWrapper}>
                <table style={styles.table}>
                  <thead>
                    <tr>
                      <th style={styles.th}>Account Name</th>
                      <th style={styles.th}>Keyword</th>
                      <th style={styles.th}>Extracted Executive</th>
                      <th style={styles.th}>Roles Found</th>
                      <th style={styles.th}>Status</th>
                      <th style={styles.th}>Article</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredResults.map((row, idx) => (
                      <tr key={idx} style={idx % 2 === 0 ? styles.trEven : styles.trOdd}>
                        <td style={styles.td}><strong>{row['Account Name']}</strong></td>
                        <td style={styles.td}>
                          <span style={styles.badgeKeyword}>{row['Growth Keyword']}</span>
                        </td>
                        <td style={{ ...styles.td, color: row['Extracted Name'] ? '#0070f3' : '#888' }}>
                          {row['Extracted Name'] || '—'}
                        </td>
                        <td style={styles.td}>{row['Roles Found'] || '—'}</td>
                        <td style={styles.td}>
                          <span style={getStatusBadgeStyle(row['Status'])}>
                            {row['Status']}
                          </span>
                        </td>
                        <td style={styles.td}>
                          {row['Article URL'] ? (
                            <a
                              href={row['Article URL']}
                              target="_blank"
                              rel="noreferrer"
                              style={styles.link}
                              title={row['Article Title']}
                            >
                              {row['Article Title'] ? truncate(row['Article Title'], 35) : 'View Article'}
                            </a>
                          ) : (
                            '—'
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const getStatusBadgeStyle = (status) => {
  const base = {
    padding: '4px 8px',
    borderRadius: '4px',
    fontSize: '0.75rem',
    fontWeight: 'bold',
    display: 'inline-block',
  };
  if (status === 'OK') return { ...base, backgroundColor: '#d1fae5', color: '#065f46' };
  if (status === 'NO_NAME_FOUND') return { ...base, backgroundColor: '#fef3c7', color: '#92400e' };
  if (status === 'NO_RELEVANT_NEWS') return { ...base, backgroundColor: '#e5e7eb', color: '#374151' };
  return { ...base, backgroundColor: '#fee2e2', color: '#991b1b' };
};

const truncate = (str, n) => (str.length > n ? str.substr(0, n - 1) + '...' : str);

const styles = {
  container: {
    padding: '2rem',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    backgroundColor: '#f8fafc',
    minHeight: '100vh',
    color: '#0f172a',
  },
  header: { marginBottom: '2rem' },
  title: { fontSize: '1.8rem', fontWeight: 'bold', margin: '0 0 0.5rem 0' },
  subtitle: { color: '#64748b', margin: 0 },
  layout: { display: 'grid', gridTemplateColumns: '320px 1fr', gap: '1.5rem' },
  card: {
    backgroundColor: '#ffffff',
    padding: '1.5rem',
    borderRadius: '8px',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
  },
  cardTitle: { fontSize: '1.1rem', fontWeight: '600', margin: '0 0 1rem 0' },
  form: { display: 'flex', flexDirection: 'column', gap: '1rem' },
  field: { display: 'flex', flexDirection: 'column', gap: '0.4rem' },
  label: { fontSize: '0.85rem', fontWeight: '600', color: '#334155' },
  input: {
    padding: '0.6rem',
    border: '1px solid #cbd5e1',
    borderRadius: '6px',
    fontSize: '0.9rem',
  },
  fileInput: { fontSize: '0.85rem' },
  fileHint: { fontSize: '0.75rem', color: '#059669' },
  checkboxField: { display: 'flex', alignItems: 'center', gap: '0.5rem' },
  checkboxLabel: { fontSize: '0.85rem', color: '#334155' },
  button: {
    padding: '0.75rem',
    backgroundColor: '#2563eb',
    color: 'white',
    border: 'none',
    borderRadius: '6px',
    fontWeight: '600',
    cursor: 'pointer',
    marginTop: '0.5rem',
  },
  buttonDisabled: { backgroundColor: '#94a3b8', cursor: 'not-allowed' },
  stopButton: {
    padding: '0.75rem',
    backgroundColor: '#dc2626',
    color: 'white',
    border: 'none',
    borderRadius: '6px',
    fontWeight: '600',
    cursor: 'pointer',
    marginTop: '0.5rem',
  },
  checkpointBox: {
    marginTop: '1rem',
    padding: '0.75rem',
    backgroundColor: '#eff6ff',
    borderRadius: '6px',
    fontSize: '0.85rem',
  },
  checkpointLink: {
    color: '#2563eb',
    fontWeight: 600,
    textDecoration: 'none',
  },
  errorBox: {
    marginTop: '1rem',
    padding: '0.75rem',
    backgroundColor: '#fef2f2',
    color: '#991b1b',
    borderRadius: '6px',
    fontSize: '0.85rem',
  },
  mainContent: { display: 'flex', flexDirection: 'column', gap: '1rem' },
  loadingCard: {
    backgroundColor: '#ffffff',
    padding: '3rem',
    borderRadius: '8px',
    textAlign: 'center',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
  },
  spinner: {
    width: '40px',
    height: '40px',
    border: '4px solid #f3f3f3',
    borderTop: '4px solid #2563eb',
    borderRadius: '50%',
    margin: '0 auto 1rem auto',
    animation: 'spin 1s linear infinite',
  },
  emptyCard: {
    backgroundColor: '#ffffff',
    padding: '3rem',
    borderRadius: '8px',
    textAlign: 'center',
    border: '2px dashed #cbd5e1',
  },
  resultsHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: '1rem',
  },
  exportButton: {
    padding: '0.5rem 1rem',
    backgroundColor: '#10b981',
    color: 'white',
    border: 'none',
    borderRadius: '6px',
    fontWeight: '600',
    cursor: 'pointer',
  },
  filterBar: { display: 'flex', gap: '1rem', marginBottom: '1rem' },
  tableWrapper: { overflowX: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.85rem' },
  th: {
    backgroundColor: '#f1f5f9',
    padding: '0.75rem',
    borderBottom: '2px solid #cbd5e1',
    color: '#475569',
  },
  td: { padding: '0.75rem', borderBottom: '1px solid #e2e8f0' },
  trEven: { backgroundColor: '#ffffff' },
  trOdd: { backgroundColor: '#f8fafc' },
  badgeKeyword: {
    backgroundColor: '#e0f2fe',
    color: '#0369a1',
    padding: '2px 6px',
    borderRadius: '4px',
  },
  link: { color: '#2563eb', textDecoration: 'none' },
};