import { generateShort } from './api';
import { useState } from 'react';
import OutputCard from './output_card';
import ProgressBar from './progressbar';
function AIShortGeneratorCard() {
  const [videoUrl, setVideoUrl] = useState('');
  const [shortUrl, setShortUrl] = useState('');
  const [title, setTitle] = useState('');
  const [duration, setDuration] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async () => {
    setError('');
    setShortUrl('');
    const response = await generateShort(videoUrl);
    if (response.short_url) {
      setShortUrl(response.short_url);
      console.log(response.short_url)
      setTitle(response.title);
      setDuration(response.duration);
    } else {
      setError(response.error || "Failed to generate short");
    }
  };

  return (
    <>
      <div className="w-full md:w-96 bg-gradient-to-br from-gray-900 via-gray-800 to-gray-900 
                    border border-cyan-500 rounded-2xl shadow-lg p-6 transition-all duration-500 
                    hover:shadow-cyan-400/30 hover:scale-105">
        
        <h1 className="text-4xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-cyan-300 to-blue-500 text-center">
          🎥 AI Cinematic Shorts
        </h1>
        <p className="text-gray-400 text-sm text-center mt-2">
          Transform any YouTube video into a <strong>high-impact short</strong> using AI-powered editing.
        </p>

        <div className="mt-6">
          <input 
            type="text"
            placeholder="🎬 Paste YouTube Link..."
            className="w-full px-4 py-3 bg-gray-800 border border-cyan-400 rounded-xl 
                      text-white placeholder-cyan-300 shadow-md focus:border-cyan-300 
                      focus:outline-none transition-all duration-500 hover:scale-105"
            onChange={(e) => setVideoUrl(e.target.value)}
            value={videoUrl}
          />
        </div>

        <button
          onClick={handleSubmit}
          className="w-full mt-4 py-3 border-2 border-cyan-500 text-white rounded-full font-medium 
                    tracking-wide transition-all duration-300 hover:scale-105 
                    hover:shadow-lg hover:shadow-cyan-400/20 bg-gradient-to-r 
                    from-cyan-300 to-blue-500 relative overflow-hidden group">
          <span className="relative z-10">✨ Generate AI Short</span>
          <span className="absolute inset-0 bg-gradient-to-r from-cyan-300 to-blue-500 
                          transform scale-x-0 group-hover:scale-x-100 transition-transform origin-left duration-300"></span>
        </button>

        {error && <p className="text-red-500 text-sm text-center mt-2">{error}</p>}
        <div className="text-center mt-6 text-gray-500 text-xs">
          Powered by AI video processing & deep-learning optimization.
        </div>
      </div>

      {shortUrl && (
        <div className="mt-10">
          <OutputCard shortUrl={shortUrl} title={title} duration={duration} />
        </div>
      )}
    </>
  );
}

export default AIShortGeneratorCard;
