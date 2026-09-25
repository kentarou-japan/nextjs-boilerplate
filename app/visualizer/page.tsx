import type { Metadata } from "next";
import Visualizer from "./Visualizer";

export const metadata: Metadata = {
  title: "Sound Art — 音で描く生成アート",
  description: "音に反応して呼吸する、訪れるたびに生まれ変わる生成アート",
};

export default function Page() {
  return <Visualizer />;
}
