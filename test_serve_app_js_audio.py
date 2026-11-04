"""Live view audio controls (toggle, button state, and autoplay fallback)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


def audio_source(source):
    start = source.index("// ── audio direct (Live View)")
    return source[start:source.index("// ── fin audio direct (Live View)", start)]


class LiveViewAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node unavailable")
        cls.source = Path(__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        cls.code = audio_source(cls.source)

    def run_js(self, scenario):
        script = r"""
const assert=require("node:assert/strict");
const videos=[];
const tick=()=>new Promise(resolve=>setImmediate(resolve));
class Video {
  constructor() {
    this.isConnected=true;this._muted=false;this._volume=1;this.events={};
    this.defaultMuted=false;this.plays=0;this.blockOnce=false;this.failure=null;
    this.classList={classes:new Set(),toggle(c,v){if(v)this.classes.add(c);else this.classes.delete(c);return this.classes.has(c);}};
    this.button={disabled:false,classList:this.classList,setAttribute(){},textContent:"",title:""};
    videos.push(this);
  }
  addEventListener(name,callback){(this.events[name]||=[]).push(callback);}
  emit(name){for(const fn of this.events[name]||[])fn({target:this});}
  get muted(){return this._muted;}
  set muted(v){if(this._muted!==v){this._muted=v;queueMicrotask(()=>this.emit("volumechange"));}}
  get volume(){return this._volume;}
  set volume(v){if(this._volume!==v){this._volume=v;queueMicrotask(()=>this.emit("volumechange"));}}
  closest(){return this.live?{querySelector:()=>this.button}:null;}
  play(){this.plays++;if(this.failure)return Promise.reject(this.failure);
    if(this.blockOnce){this.blockOnce=false;return Promise.reject(Object.assign(new Error("blocked"),{name:"NotAllowedError"}));}
    return Promise.resolve();}
}
const document={querySelectorAll:()=>videos.filter(v=>v.isConnected)};
const $=id=>({querySelector:()=>videos.at(-1)});
const h=String,t=String,cssId=String;
let system={camera_audio_enabled:true};
"""
        script += self.code + "\n"
        script += r"""
(async()=>{
 const first=new Video();
 first.live=true;
 preparerAudioVideo(first);
 await tick();
 assert.equal(first.muted,true);
 assert.equal(first.defaultMuted,true);

 if(SCENARIO==="initial"){
   assert.equal(first.button.textContent,"live.audio.waiting");
   assert.equal(first.button.disabled,true);
 }else if(SCENARIO==="speaker"){
   first.blinkAudioDisponible=true;
   actualiserBoutonAudio(first);
   assert.equal(first.button.textContent,"live.audio.listen");
   assert.equal(first.button.disabled,false);
   assert.equal(first.classList.classes.has("active"),false);

   // Toggle unmute (listen)
   toggleAudio("Cam");
   await tick();
   assert.equal(first.muted,false);
   assert.equal(first.button.textContent,"live.audio.mute");
   assert.equal(first.classList.classes.has("active"),true);

   // Toggle mute
   toggleAudio("Cam");
   await tick();
   assert.equal(first.muted,true);
   assert.equal(first.button.textContent,"live.audio.listen");
   assert.equal(first.classList.classes.has("active"),false);

   // Unavailable audio
   first.blinkAudioDisponible=false;
   actualiserBoutonAudio(first);
   assert.equal(first.button.disabled,true);
   assert.equal(first.button.textContent,"live.audio.unavailable");
 }else if(SCENARIO==="autoplay"){
   first.blinkAudioDisponible=true;
   actualiserBoutonAudio(first);
   first.muted=false;
   first.blockOnce=true;
   await lireVideo(first);
   await tick();
   assert.equal(first.muted,true);
   assert.equal(first.plays,2);
   assert.equal(first.button.textContent,"live.audio.listen");
 }else if(SCENARIO==="failure"){
   first.failure=new Error("decode failed");
   await assert.rejects(lireVideo(first),/decode failed/);
   assert.equal(first.muted,true);
 }
 console.log(JSON.stringify({ok:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SCENARIO", json.dumps(scenario))
        result = subprocess.run([self.node, "-"], input=script, text=True,
                                encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_live_initial_state_is_muted(self): self.run_js("initial")
    def test_live_speaker_toggle_and_button_states(self): self.run_js("speaker")
    def test_live_autoplay_rejection_falls_back_to_muted(self): self.run_js("autoplay")
    def test_live_media_error_is_reterror(self): self.run_js("failure")


if __name__ == "__main__":
    unittest.main()

