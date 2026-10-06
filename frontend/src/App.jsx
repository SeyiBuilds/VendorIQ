import { useEffect, useState } from "react";
import { listMatches, runMatch } from "./api";
import MatchPicker from "./components/MatchPicker";
import PipelineStatus from "./components/PipelineStatus";
import NarrativePanel from "./components/NarrativePanel";
import CorruptionPanel from "./components/CorruptionPanel";
import "./App.css";

export default function App() {
  const [matches, setMatches] = useState([]);
  const [selectedMatchId, setSelectedMatchId] = useState("");
  const [selectedVendor, setSelectedVendor] = useState("opta");
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState(null);
  const [cleanConfidence, setCleanConfidence] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    listMatches()
      .then((data) => {
        setMatches(data);
        if (data.length) setSelectedMatchId(data[0].match_id);
      })
      .catch((e) => setError(`Could not reach the API: ${e.message}`));
  }, []);

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await runMatch(selectedMatchId, selectedVendor);
      setRunResult(result);
      setCleanConfidence(result.document?.metadata?.quality_confidence ?? null);
    } catch (e) {
      setError(e.message);
      setRunResult(null);
    } finally {
      setRunning(false);
    }
  };

  const handleCorruptResult = (result) => {
    setRunResult(result);
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>Synthetic Match Insights Engine</h1>
        <p className="tagline">
          Inside the Game: Developer Hackathon — turning synthetic football events into explainable
          match intelligence, in real time.
        </p>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <MatchPicker
        matches={matches}
        selectedMatchId={selectedMatchId}
        selectedVendor={selectedVendor}
        onChange={({ matchId, vendor }) => {
          setSelectedMatchId(matchId);
          setSelectedVendor(vendor);
        }}
        onRun={handleRun}
        running={running}
      />

      <CorruptionPanel
        matchId={selectedMatchId}
        onResult={handleCorruptResult}
        cleanConfidence={cleanConfidence}
      />

      {runResult && (
        <>
          <PipelineStatus
            orchestrationLog={runResult.orchestration_log}
            overallStatus={runResult.overall_status}
            confidence={runResult.document?.metadata?.quality_confidence}
          />
          <NarrativePanel narrative={runResult.narrative} />
        </>
      )}
    </div>
  );
}
