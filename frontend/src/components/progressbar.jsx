import React, { useState, useEffect } from 'react';

function ProgressBar({ progress = 0 }) {
  const [animatedProgress, setAnimatedProgress] = useState(0);

  useEffect(() => {
    setAnimatedProgress(progress);
  }, [progress]);

  return (
    <div className='w-full bg-gradient-to-br from-gray-900 via-gray-800 to-gray-900 
                    border border-cyan-500 rounded-2xl shadow-lg p-6 transition-all duration-500 
                    hover:shadow-cyan-400/30 hover:scale-105'>

        {/* Title */}
        <h1 className='text-3xl font-bold text-transparent mb-2 bg-clip-text bg-gradient-to-r from-cyan-300 to-blue-500 text-center'>
            AI Processing Progress
        </h1>

        {/* Status Message */}
        <p className='text-center text-gray-400 text-sm mb-4'>
            Generating your AI-powered short... Please wait! 🚀
        </p>

        {/* Progress Bar Wrapper */}
        <div className="w-[48rem] max-w-full bg-gray-800 rounded-lg overflow-hidden shadow-lg relative">
            
            {/* Dynamic Progress Bar */}
            <div 
                className="h-4 bg-gradient-to-r from-cyan-400 to-blue-500 transition-all duration-500"
                style={{ width: `${animatedProgress}%` }}
            />

            {/* Percentage Indicator */}
            <p className="absolute w-full text-center text-white font-medium mt-1">
                {animatedProgress.toFixed(0)}%
            </p>
        </div>
    </div>
  );
}

export default ProgressBar;
