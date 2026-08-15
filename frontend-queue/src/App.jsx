import { useState, useEffect } from 'react'
import { db } from './firebase'
import { collection, addDoc, query, orderBy, onSnapshot, serverTimestamp, limit } from 'firebase/firestore'

function App() {
  const [password, setPassword] = useState('')
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [url, setUrl] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [queue, setQueue] = useState([])
  const [loading, setLoading] = useState(false)

  // Simple password check
  const handleLogin = (e) => {
    e.preventDefault()
    if (password === 'Taylor') {
      setIsAuthenticated(true)
      localStorage.setItem('auth', 'true')
    } else {
      alert('Incorrect password')
    }
  }

  useEffect(() => {
    if (localStorage.getItem('auth') === 'true') {
      setIsAuthenticated(true)
    }
  }, [])

  // Queue listener
  useEffect(() => {
    if (!isAuthenticated) return

    const q = query(
      collection(db, 'queue'),
      orderBy('created_at', 'desc'),
      limit(20)
    )

    const unsubscribe = onSnapshot(q, (snapshot) => {
      const items = []
      snapshot.forEach((doc) => {
        items.push({ id: doc.id, ...doc.data() })
      })
      setQueue(items)
    })

    return () => unsubscribe()
  }, [isAuthenticated])

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!url || !templateId) return

    setLoading(true)
    try {
      await addDoc(collection(db, 'queue'), {
        url,
        template_id: templateId,
        status: 'pending',
        created_at: serverTimestamp(),
        auto: true
      })
      setUrl('')
      setTemplateId('')
    } catch (error) {
      console.error("Error adding document: ", error)
      alert("Error adding to queue")
    }
    setLoading(false)
  }

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-100">
        <form onSubmit={handleLogin} className="bg-white p-8 rounded shadow-md w-80">
          <h1 className="text-2xl mb-4 font-bold text-center">Queue Login</h1>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter Password"
            className="w-full p-2 border rounded mb-4"
          />
          <button type="submit" className="w-full bg-blue-500 text-white p-2 rounded hover:bg-blue-600">
            Login
          </button>
        </form>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-100 p-4">
      <div className="max-w-4xl mx-auto">
        <div className="flex justify-between items-center mb-8">
          <h1 className="text-3xl font-bold text-gray-800">Templatea Queue</h1>
          <button
            onClick={() => {
              setIsAuthenticated(false)
              localStorage.removeItem('auth')
            }}
            className="text-red-500 hover:text-red-700"
          >
            Logout
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          {/* Submission Form */}
          <div className="bg-white p-6 rounded-lg shadow-md h-fit">
            <h2 className="text-xl font-semibold mb-4">Add to Queue</h2>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Instagram URL</label>
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://instagram.com/..."
                  className="w-full p-2 border rounded focus:ring-2 focus:ring-blue-500 outline-none"
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Template ID</label>
                <input
                  type="text"
                  value={templateId}
                  onChange={(e) => setTemplateId(e.target.value)}
                  placeholder="e.g. marketing_spots"
                  className="w-full p-2 border rounded focus:ring-2 focus:ring-blue-500 outline-none"
                  required
                />
              </div>
              <button
                type="submit"
                disabled={loading}
                className={`w-full text-white p-2 rounded font-medium ${loading ? 'bg-gray-400' : 'bg-blue-600 hover:bg-blue-700'
                  }`}
              >
                {loading ? 'Adding...' : 'Add to Queue'}
              </button>
            </form>
          </div>

          {/* Queue List */}
          <div className="bg-white p-6 rounded-lg shadow-md">
            <h2 className="text-xl font-semibold mb-4">Recent Queue</h2>
            <div className="space-y-3">
              {queue.length === 0 ? (
                <p className="text-gray-500 text-center py-4">Queue is empty</p>
              ) : (
                queue.map((item) => (
                  <div key={item.id} className="border-b pb-3 last:border-0">
                    <div className="flex justify-between items-start mb-1">
                      <span className={`text-xs font-bold px-2 py-1 rounded uppercase ${item.status === 'completed' ? 'bg-green-100 text-green-800' :
                        item.status === 'processing' ? 'bg-blue-100 text-blue-800' :
                          item.status === 'failed' ? 'bg-red-100 text-red-800' :
                            'bg-yellow-100 text-yellow-800'
                        }`}>
                        {item.status}
                      </span>
                      <span className="text-xs text-gray-400">
                        {item.created_at?.seconds ? new Date(item.created_at.seconds * 1000).toLocaleTimeString() : 'Just now'}
                      </span>
                    </div>
                    <p className="text-sm text-gray-800 truncate mb-1" title={item.url}>{item.url}</p>
                    <div className="flex justify-between text-xs text-gray-500">
                      <span>{item.template_id}</span>
                      {item.workspace_id && <span>WS: {item.workspace_id}</span>}
                    </div>
                    {item.error && (
                      <p className="text-xs text-red-500 mt-1 bg-red-50 p-1 rounded">{item.error}</p>
                    )}
                    {item.output_link && (
                      <a
                        href={item.output_link}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="block mt-2 text-center bg-green-600 text-white text-xs py-1 px-2 rounded hover:bg-green-700 font-bold"
                      >
                        Download Video
                      </a>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default App
