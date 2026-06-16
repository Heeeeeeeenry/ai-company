"""Auto-Evolution Tests"""

import pytest
import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestExperienceStore:
    def test_record_and_query(self):
        """Experience should be recordable and queryable."""
        from src.evolution.engine import ExperienceStore, ExperienceRecord
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            store = ExperienceStore(storage_path=path)
            
            store.record(ExperienceRecord(
                task="写一个API", department="developer",
                task_type="DEVELOPMENT", auditor_score=80,
                pmo_score=90, final_score=83, retries=0,
                verdict="APPROVE",
            ))
            store.record(ExperienceRecord(
                task="审查代码", department="developer",
                task_type="CODE_REVIEW", auditor_score=45,
                pmo_score=55, final_score=48, retries=3,
                verdict="FORCE_APPROVE",
            ))
            
            assert store.count() == 2
            
            # Query by department
            dev_tasks = store.query(department="developer")
            assert len(dev_tasks) == 2
            
            # Query by score
            low_tasks = store.query(max_score=50)
            assert len(low_tasks) == 1
            assert low_tasks[0].task_type == "CODE_REVIEW"
            
            # Stats
            stats = store.get_stats()
            assert stats["total_tasks"] == 2
            assert 60 < stats["avg_score"] < 70
            
        finally:
            os.unlink(path)

    def test_empty_store(self):
        """Empty store should return sensible defaults."""
        from src.evolution.engine import ExperienceStore
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            store = ExperienceStore(storage_path=path)
            assert store.count() == 0
            stats = store.get_stats()
            assert stats["total_tasks"] == 0
        finally:
            os.unlink(path)


class TestPatternAnalyzer:
    def test_insufficient_data(self):
        """With <3 records, should return NOT_ENOUGH_DATA."""
        from src.evolution.engine import ExperienceStore, PatternAnalyzer, ExperienceRecord
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            store = ExperienceStore(storage_path=path)
            analyzer = PatternAnalyzer(store)
            
            insights = analyzer.analyze()
            assert len(insights) == 1
            assert insights[0].insight_type == "NOT_ENOUGH_DATA"
            
            # Add 3 records
            for i in range(3):
                store.record(ExperienceRecord(
                    task=f"task{i}", department="developer",
                    task_type="DEVELOPMENT", auditor_score=50,
                    pmo_score=50, final_score=50, retries=0,
                    verdict="REVISE",
                ))
            
            insights = analyzer.analyze()
            # Should have real insights
            types = [i.insight_type for i in insights]
            assert "NOT_ENOUGH_DATA" not in types, f"Got: {types}"
            
        finally:
            os.unlink(path)

    def test_department_performance(self):
        """Underperforming departments should be flagged."""
        from src.evolution.engine import ExperienceStore, PatternAnalyzer, ExperienceRecord
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            store = ExperienceStore(storage_path=path)
            analyzer = PatternAnalyzer(store)
            
            # Researcher constantly fails
            for i in range(5):
                store.record(ExperienceRecord(
                    task=f"research {i}", department="researcher",
                    task_type="RESEARCH", auditor_score=40,
                    pmo_score=30, final_score=37, retries=3,
                    verdict="FORCE_APPROVE",
                ))
            
            insights = analyzer.analyze()
            prompt_insights = [i for i in insights if i.insight_type == "PROMPT_WEAKNESS"]
            assert len(prompt_insights) > 0
            assert any("researcher" in str(i.affected_role) for i in prompt_insights)
            
        finally:
            os.unlink(path)


class TestAdaptationEngine:
    def test_evolve_cycle(self):
        """A full evolution cycle should produce results."""
        from src.evolution.engine import ExperienceStore, PatternAnalyzer, AdaptationEngine, ExperienceRecord
        
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        
        try:
            store = ExperienceStore(storage_path=path)
            analyzer = PatternAnalyzer(store)
            engine = AdaptationEngine(store, analyzer)
            
            # Add failing tasks
            for i in range(8):
                store.record(ExperienceRecord(
                    task=f"task {i}", department="researcher",
                    task_type="RESEARCH", auditor_score=35,
                    pmo_score=25, final_score=32, retries=3,
                    verdict="FORCE_APPROVE", peak_retry_score=40,
                ))
            
            result = engine.evolve()
            assert result["insights_total"] > 0
            assert "stats" in result
            
        finally:
            os.unlink(path)


class TestEvolutionIntegration:
    def test_record_completed_task(self):
        """Convenience function should work."""
        from src.evolution.engine import record_completed_task, get_experience_store
        
        # Reset store for clean test
        import src.evolution.engine as engine
        engine._experience_store = None
        engine._adaptation_engine = None
        
        store = get_experience_store()
        initial_count = store.count()
        
        task_id = record_completed_task(
            task="测试任务", department="developer",
            task_type="DEVELOPMENT", auditor_score=85,
            pmo_score=90, final_score=86.5, retries=0,
            verdict="APPROVE",
        )
        
        assert store.count() == initial_count + 1
        assert task_id is not None
        
        # Clean up
        engine._experience_store = None
        engine._adaptation_engine = None
