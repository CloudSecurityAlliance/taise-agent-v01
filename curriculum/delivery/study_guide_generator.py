"""TAISE-Agent v0.5 - Study Guide Generator

Convenience wrapper around CurriculumEngine for generating study guides
in various delivery formats.
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from curriculum.curriculum_engine import CurriculumEngine


def generate_study_guide(curriculum_dir="curriculum", delivery_mode="document_upload", output_path=None):
    """Generate a study guide from the curriculum modules.

    Args:
        curriculum_dir: Path to the curriculum directory containing manifest and modules.
        delivery_mode: One of 'document_upload', 'system_prompt', or 'api_payload'.
        output_path: Optional file path to write the guide. If None, returns content only.

    Returns:
        The study guide content as a string.
    """
    engine = CurriculumEngine(curriculum_dir=curriculum_dir)
    content = engine.generate_study_guide(delivery_mode=delivery_mode)

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            f.write(content)

    return content


def deliver_and_record(agent_profile, curriculum_dir="curriculum", output_dir="runs"):
    """Deliver the curriculum to an agent and save the delivery record.

    Args:
        agent_profile: Dict with agent_name, agent_type, curriculum_delivery, etc.
        curriculum_dir: Path to the curriculum directory.
        output_dir: Directory to save the delivery record.

    Returns:
        Tuple of (delivery_record dict, record_filepath str).
    """
    engine = CurriculumEngine(curriculum_dir=curriculum_dir)
    record = engine.deliver_curriculum(agent_profile)
    filepath = engine.save_curriculum_record(record, output_dir)
    return record, filepath


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate TAISE-Agent study guide")
    parser.add_argument("--curriculum-dir", default="curriculum", help="Path to curriculum directory")
    parser.add_argument("--mode", default="document_upload",
                        choices=["document_upload", "system_prompt", "api_payload"],
                        help="Delivery mode for the study guide")
    parser.add_argument("--output", default=None, help="Output file path")
    args = parser.parse_args()

    content = generate_study_guide(
        curriculum_dir=args.curriculum_dir,
        delivery_mode=args.mode,
        output_path=args.output
    )

    if not args.output:
        print(content)
