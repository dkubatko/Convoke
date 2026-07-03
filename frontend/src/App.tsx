import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { useToast } from './components/Toast'
import { LoadingWire } from './components/ui'
import { api, UNAUTHORIZED_EVENT } from './lib/api'
import Bots from './pages/Bots'
import ChatDetail from './pages/ChatDetail'
import Chats from './pages/Chats'
import Login from './pages/Login'
import Models from './pages/Models'
import Overview from './pages/Overview'
import Tools from './pages/Tools'
import Workflows from './pages/Workflows'

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)
  const toast = useToast()

  useEffect(() => {
    api
      .get('/api/auth/me')
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false))
  }, [])

  useEffect(() => {
    const onUnauthorized = () => {
      setAuthed((was) => {
        if (was) toast('info', 'Your session expired — sign in again.')
        return false
      })
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [toast])

  if (authed === null) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <LoadingWire />
      </div>
    )
  }

  if (!authed) {
    return (
      <Routes>
        <Route path="*" element={<Login onLogin={() => setAuthed(true)} />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route element={<Layout onSignOut={() => setAuthed(false)} />}>
        <Route index element={<Overview />} />
        <Route path="bots" element={<Bots />} />
        <Route path="chats" element={<Chats />} />
        <Route path="chats/:chatId" element={<ChatDetail />} />
        <Route path="workflows" element={<Workflows />} />
        <Route path="tools" element={<Tools />} />
        <Route path="models" element={<Models />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
