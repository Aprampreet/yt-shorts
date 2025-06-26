import React from 'react';

function OutputCard({ shortUrl, title, duration }) {
  return (
    <div className="card w-full max-w-xl bg-gradient-to-br from-gray-900 via-gray-800 to-gray-900 
                    border border-cyan-500 rounded-2xl shadow-lg p-6 transition-all duration-500 
                    hover:shadow-cyan-400/30 hover:scale-105">
      
      {/* Title Section */}
      <h1 className="text-3xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-cyan-300 to-blue-500 text-center">
        🎬 {title || "Your AI-Generated Short"}
      </h1>

      {/* Metadata */}
      {duration && (
        <p className="text-gray-400 text-sm text-center mt-1">
          ⏱ {duration} seconds
        </p>
      )}

      {/* Video Display */}
      <div className="mt-4 rounded-xl overflow-hidden shadow-md">
        <video src={shortUrl} controls autoPlay loop muted className="w-full rounded-lg"></video>
      </div>

      <button
  onClick={() => {
    const link = document.createElement("a");
    link.href = shortUrl;
    link.setAttribute("download", "AI_Short.mp4"); 
    link.setAttribute("target", "_blank");

   
    fetch(shortUrl)
      .then(res => res.blob())
      .then(blob => {
        const blobUrl = URL.createObjectURL(blob);
        link.href = blobUrl;
        link.click();
        URL.revokeObjectURL(blobUrl);
      });

  }}
  className="w-full mt-4 py-3 border-2 border-cyan-500 text-white rounded-full font-medium 
             tracking-wide transition-all duration-300 hover:scale-105 
             hover:shadow-lg hover:shadow-cyan-400/20 bg-gradient-to-r 
             from-cyan-300 to-blue-500 relative overflow-hidden group">
  <span className="relative z-10">🔗 Download Short</span>
  <span className="absolute inset-0 bg-gradient-to-r from-cyan-300 to-blue-500 
                   transform scale-x-0 group-hover:scale-x-100 transition-transform origin-left duration-300"></span>
</button>


    </div>
  );
}

export default OutputCard;
