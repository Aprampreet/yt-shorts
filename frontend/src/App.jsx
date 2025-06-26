import OutputCard from './components/output_card';
import ProgressBar from './components/progressbar';
import AIShortGeneratorCard from './components/input_card';
function App() {
  return (
    <div className="min-h-screen flex flex-col gap-10 justify-center items-center 
                    bg-gradient-to-r from-blue-900 to-slate-900 p-6">

      {/* Card Container - Wrap Input & Output Cards */}
      <div className="flex flex-row gap-6">
        <AIShortGeneratorCard />
        

      </div>

      {/* Progress Bar - Below Cards, Full Width */}
      

    </div>
  );
}

export default App;
