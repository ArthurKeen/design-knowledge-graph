#!/usr/bin/env python3
"""
Author ETL Pipeline
===================
Extracts author information from Git commits and creates:
1. Author vertices (unique contributors)
2. AUTHORED edges (author -> commit)
3. MAINTAINS edges (author -> module, based on commit frequency)
"""

import re
from datetime import datetime, timedelta
from collections import defaultdict
from db_utils import get_db

def parse_git_author(author_string):
    """
    Parse git author string into name and email.
    
    Examples:
        "Alice Smith <alice.smith@example.com>"
        "bob@example.com"
        "Charlie"
    
    Returns:
        dict with 'name' and 'email' keys
    """
    # Pattern: "Name <email>" or just "email" or just "Name"
    match = re.match(r'^([^<]+?)\s*<([^>]+)>$', author_string.strip())
    
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip()
    elif '@' in author_string:
        # Just an email
        email = author_string.strip()
        name = email.split('@')[0].replace('.', ' ').title()
    else:
        # Just a name
        name = author_string.strip()
        email = f"{name.lower().replace(' ', '.')}@unknown"
    
    return {'name': name, 'email': email}


def normalize_email(email):
    """
    Create a normalized key from an email address.
    
    Examples:
        alice.smith@example.com -> alice_smith
        bob@example.com -> bob
    """
    username = email.split('@')[0]
    # Replace dots and special chars with underscores
    normalized = re.sub(r'[^a-z0-9]', '_', username.lower())
    # Remove consecutive underscores
    normalized = re.sub(r'_+', '_', normalized)
    # Remove leading/trailing underscores
    normalized = normalized.strip('_')
    return normalized


def extract_authors_from_commits(db):
    """
    Phase 1: Extract unique authors from all GitCommit documents.
    
    Returns:
        dict: {author_key: author_info}
    """
    print("\n[Phase 1] Extracting authors from commits...")
    
    commits_col = db.collection('GitCommit')
    authors = {}
    
    for commit in commits_col.all():
        author_string = commit.get('metadata', {}).get('author', '')
        if not author_string:
            continue
        
        author_info = parse_git_author(author_string)
        email = author_info['email']
        name = author_info['name']
        key = normalize_email(email)
        
        # Convert Unix timestamp to ISO format if needed
        timestamp = commit.get('metadata', {}).get('timestamp')
        if timestamp and isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(timestamp).isoformat() + 'Z'
        
        if key not in authors:
            authors[key] = {
                '_key': key,
                'name': name,
                'email': email,
                'email_variations': [author_string],
                'commit_ids': [commit['_id']],
                'first_seen': timestamp,
                'last_seen': timestamp
            }
        else:
            # Update existing author
            authors[key]['commit_ids'].append(commit['_id'])
            authors[key]['email_variations'].append(author_string)
            
            # Update timestamps
            if timestamp:
                if timestamp < authors[key]['first_seen']:
                    authors[key]['first_seen'] = timestamp
                if timestamp > authors[key]['last_seen']:
                    authors[key]['last_seen'] = timestamp
    
    print(f"  Found {len(authors)} unique authors")
    for key, author in list(authors.items())[:5]:
        print(f"    - {author['name']}: {len(author['commit_ids'])} commits")
    if len(authors) > 5:
        print(f"    ... and {len(authors) - 5} more")
    
    return authors


def is_active(last_seen_timestamp, threshold_days=180):
    """
    Determine if an author is still active.
    
    Args:
        last_seen_timestamp: ISO timestamp string
        threshold_days: Days since last commit to consider active
    
    Returns:
        bool: True if active
    """
    if not last_seen_timestamp:
        return False
    
    try:
        last_seen = datetime.fromisoformat(last_seen_timestamp.replace('Z', '+00:00'))
        now = datetime.now(last_seen.tzinfo)
        days_since = (now - last_seen).days
        return days_since <= threshold_days
    except Exception:
        return False


def create_author_vertices(db, authors):
    """
    Phase 2: Create Author collection and insert documents.
    """
    print("\n[Phase 2] Creating Author vertices...")
    
    # Create collection if it doesn't exist
    if not db.has_collection('Author'):
        db.create_collection('Author')
        print("  Created collection: Author")
    else:
        print("  Collection already exists: Author")
    
    author_col = db.collection('Author')
    
    inserted = 0
    updated = 0
    
    for key, author_data in authors.items():
        # Deduplicate email variations
        unique_variations = list(set(author_data['email_variations']))
        
        author_doc = {
            '_key': key,
            'name': author_data['name'],
            'email': author_data['email'],
            'email_variations': unique_variations,
            'metadata': {
                'first_seen': author_data['first_seen'],
                'last_seen': author_data['last_seen'],
                'total_commits': len(author_data['commit_ids']),
                'active': is_active(author_data['last_seen']),
                'team': None,  # Can be enriched manually later
                'role': None,  # Can be enriched manually later
                'expertise_areas': []  # Will be derived from MAINTAINS edges
            }
        }
        
        # Check if author already exists
        if author_col.has(key):
            author_col.update({'_key': key, **author_doc})
            updated += 1
        else:
            author_col.insert(author_doc)
            inserted += 1
    
    print(f"  Inserted: {inserted}, Updated: {updated}")
    return True


def create_authored_edges(db, authors):
    """
    Phase 3: Create AUTHORED edges from Author to GitCommit.
    """
    print("\n[Phase 3] Creating AUTHORED edges...")
    
    # Create edge collection if it doesn't exist
    if not db.has_collection('AUTHORED'):
        db.create_collection('AUTHORED', edge=True)
        print("  Created edge collection: AUTHORED")
    else:
        print("  Collection already exists: AUTHORED")
    
    authored_col = db.collection('AUTHORED')
    
    # Clear existing edges (for idempotency)
    print("  Clearing existing AUTHORED edges...")
    authored_col.truncate()
    
    inserted = 0
    
    for key, author_data in authors.items():
        author_id = f"Author/{key}"
        
        for commit_id in author_data['commit_ids']:
            edge = {
                '_from': author_id,
                '_to': commit_id
            }
            
            try:
                authored_col.insert(edge)
                inserted += 1
            except Exception as e:
                print(f"  Warning: Could not create edge {author_id} -> {commit_id}: {e}")
    
    print(f"  Created {inserted} AUTHORED edges")
    return True


def calculate_maintenance_score(commit_count, total_commits, days_since_last):
    """
    Calculate a maintenance score (0-1) based on:
    - Relative commit frequency
    - Recency of activity
    
    Args:
        commit_count: Number of commits by this author to this module
        total_commits: Total commits to this module
        days_since_last: Days since author's last commit to module
    
    Returns:
        float: Score between 0 and 1
    """
    # Frequency component (0-1)
    frequency_score = min(commit_count / max(total_commits, 1), 1.0)
    
    # Recency component (0-1)
    # Decay over 365 days
    if days_since_last <= 0:
        recency_score = 1.0
    elif days_since_last >= 365:
        recency_score = 0.1
    else:
        recency_score = 1.0 - (days_since_last / 365) * 0.9
    
    # Weighted average: 60% frequency, 40% recency
    score = (frequency_score * 0.6) + (recency_score * 0.4)
    
    return round(score, 3)


def create_maintains_edges(db):
    """
    Phase 4: Create MAINTAINS edges from Author to RTL_Module.
    
    An author "maintains" a module if:
    - They have >= 3 commits to that module, OR
    - They have >= 20% of total commits to that module, OR
    - They have the most recent commit
    """
    print("\n[Phase 4] Creating MAINTAINS edges...")
    
    # Create edge collection if it doesn't exist
    if not db.has_collection('MAINTAINS'):
        db.create_collection('MAINTAINS', edge=True)
        print("  Created edge collection: MAINTAINS")
    else:
        print("  Collection already exists: MAINTAINS")
    
    maintains_col = db.collection('MAINTAINS')
    
    # Clear existing edges (for idempotency)
    print("  Clearing existing MAINTAINS edges...")
    maintains_col.truncate()
    
    # Pass 1: total commits per logical module (label+repo), computed once
    print("  Pass 1: counting total commits per logical module...")
    module_totals_q = """
    FOR e IN MODIFIED
      LET mod = DOCUMENT(e._to)
      COLLECT label = mod.label, repo = mod.repo WITH COUNT INTO cnt
      RETURN { label, repo, cnt }
    """
    module_totals = {(r["label"], r["repo"]): r["cnt"]
                     for r in db.aql.execute(module_totals_q)}
    print(f"    {len(module_totals)} logical modules with commits")

    # Pass 2: per-author per-logical-module commit counts
    # Temporal RTL_Module snapshots are unique per commit, so we group by
    # label + repo to get the logical module identity.
    query = """
    WITH Author, GitCommit, RTL_Module
    FOR author IN Author
      FOR commit IN 1..1 OUTBOUND author AUTHORED
        FOR module IN 1..1 OUTBOUND commit MODIFIED
          COLLECT author_id = author._id,
                  mod_label = module.label,
                  mod_repo  = module.repo
          INTO commits = commit
          LET commit_count = LENGTH(commits)
          LET timestamps = commits[*].timestamp
          RETURN {
            author_id,
            mod_label,
            mod_repo,
            commit_count,
            first_commit: MIN(timestamps),
            last_commit:  MAX(timestamps)
          }
    """

    print("  Pass 2: computing author-module relationships...")
    results_raw = list(db.aql.execute(query))

    # Build a lookup: (label, repo) → most-recent RTL_Module _id
    print("  Resolving logical modules to latest snapshots...")
    latest_q = """
    FOR m IN RTL_Module
      SORT m.valid_from_ts DESC
      COLLECT label = m.label, repo = m.repo INTO snapshots = m._id
      RETURN { label, repo, latest_id: snapshots[0] }
    """
    latest_module = {(r["label"], r["repo"]): r["latest_id"]
                     for r in db.aql.execute(latest_q)}

    # Apply threshold filter and merge with module totals
    results = []
    for r in results_raw:
        total = module_totals.get((r["mod_label"], r["mod_repo"]), r["commit_count"])
        pct = r["commit_count"] / total if total else 0
        if r["commit_count"] >= 3 or pct >= 0.2:
            mod_id = latest_module.get((r["mod_label"], r["mod_repo"]))
            if not mod_id:
                continue
            r["module_id"] = mod_id
            r["total_module_commits"] = total
            results.append(r)
    
    print(f"  Found {len(results)} maintenance relationships")
    
    inserted = 0
    now = datetime.now()
    
    for rel in results:
        # Calculate days since last commit
        try:
            last_commit_dt = datetime.fromisoformat(rel['last_commit'].replace('Z', '+00:00'))
            days_since = (now - last_commit_dt).days
        except Exception:
            days_since = 999
        
        # Calculate maintenance score
        score = calculate_maintenance_score(
            rel['commit_count'],
            rel['total_module_commits'],
            days_since
        )
        
        edge = {
            '_from': rel['author_id'],
            '_to': rel['module_id'],
            'commit_count': rel['commit_count'],
            'first_commit': rel['first_commit'],
            'last_commit': rel['last_commit'],
            'maintenance_score': score
        }
        
        try:
            maintains_col.insert(edge)
            inserted += 1
        except Exception as e:
            print(f"  Warning: Could not create MAINTAINS edge: {e}")
    
    print(f"  Created {inserted} MAINTAINS edges")
    
    # Show top maintainers
    print("\n  Top maintainers:")
    top_query = """
    FOR author IN Author
      LET modules = LENGTH(FOR m IN 1..1 OUTBOUND author MAINTAINS RETURN 1)
      FILTER modules > 0
      SORT modules DESC
      LIMIT 5
      RETURN {
        author: author.name,
        modules: modules,
        commits: author.metadata.total_commits
      }
    """
    
    for maintainer in db.aql.execute(top_query):
        print(f"    - {maintainer['author']}: {maintainer['modules']} modules, {maintainer['commits']} commits")
    
    return True


def main():
    """
    Main execution function.
    """
    print("="*60)
    print("Author ETL Pipeline")
    print("="*60)
    
    db = get_db()
    print(f"Connected to database: {db.name}\n")
    
    # Phase 1: Extract authors
    authors = extract_authors_from_commits(db)
    
    if not authors:
        print("\n[ERROR] No authors found in GitCommit collection")
        return False
    
    # Phase 2: Create Author vertices
    create_author_vertices(db, authors)
    
    # Phase 3: Create AUTHORED edges
    create_authored_edges(db, authors)
    
    # Phase 4: Create MAINTAINS edges
    create_maintains_edges(db)
    
    print("\n" + "="*60)
    print("Author ETL Pipeline Complete!")
    print("="*60)
    print("\nSummary:")
    print(f"  Authors: {len(authors)}")
    print(f"  AUTHORED edges: {db.collection('AUTHORED').count()}")
    print(f"  MAINTAINS edges: {db.collection('MAINTAINS').count()}")
    
    return True


if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)

