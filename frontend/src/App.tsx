import { BrowserRouter, Route, Routes } from "react-router-dom";
import { NavBar } from "./components/NavBar";
import { LiveMap } from "./pages/LiveMap";
import { Dashboard } from "./pages/Dashboard";
import { CheckMyCommute } from "./pages/CheckMyCommute";

function App() {
  return (
    <BrowserRouter>
      <NavBar />
      <Routes>
        <Route path="/" element={<LiveMap />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/commute" element={<CheckMyCommute />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
