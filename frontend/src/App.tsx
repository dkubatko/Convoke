import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api } from './lib/api'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)

  useEffect(() => {
    api
      .get('/api/auth/me')
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false))
  }, [])

  if (authed === null) {
    return <div className="centered">Loading…</div>
  }

  return (
    <Routes>
      <Route
        path="/login"
        element={authed ? <Navigate to="/" replace /> : <Login onLogin={() => setAuthed(true)} />}
      />
      <Route
        path="/*"
        element={authed ? <Dashboard onLogout={() => setAuthed(false)} /> : <Navigate to="/login" replace />}
      />
    </Routes>
  )
}
