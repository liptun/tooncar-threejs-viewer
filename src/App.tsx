import { Navigate, Route, Routes } from 'react-router-dom'
import { TrackPage } from './pages/TrackPage'
import { tracks } from './tracks'

export default function App() {
  const defaultTrackUrl = `/track/${tracks[0].id}`

  return (
    <Routes>
      <Route path="/" element={<Navigate to={defaultTrackUrl} replace />} />
      <Route path="/track/:trackId" element={<TrackPage />} />
      <Route path="*" element={<Navigate to={defaultTrackUrl} replace />} />
    </Routes>
  )
}
