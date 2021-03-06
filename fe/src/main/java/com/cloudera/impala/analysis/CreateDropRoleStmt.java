// Copyright 2014 Cloudera Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package com.cloudera.impala.analysis;

import com.cloudera.impala.catalog.Role;
import com.cloudera.impala.common.AnalysisException;
import com.cloudera.impala.thrift.TCreateDropRoleParams;
import com.google.common.base.Preconditions;

/**
 * Represents a "CREATE ROLE" or "DROP ROLE" statement.
 */
public class CreateDropRoleStmt extends AuthorizationStmt {
  private final String roleName_;
  private final boolean isDropRole_;

  // Set in analysis
  private String user_;

  public CreateDropRoleStmt(String roleName, boolean isDropRole) {
    Preconditions.checkNotNull(roleName);
    roleName_ = roleName;
    isDropRole_ = isDropRole;
  }

  @Override
  public String toSql() {
    return String.format("%s ROLE %s", roleName_, isDropRole_ ? "DROP" : "CREATE");
  }

  public TCreateDropRoleParams toThrift() {
    TCreateDropRoleParams params = new TCreateDropRoleParams();
    params.setRole_name(roleName_);
    params.setIs_drop(isDropRole_);
    return params;
  }

  @Override
  public void analyze(Analyzer analyzer) throws AnalysisException {
    super.analyze(analyzer);
    Role existingRole = analyzer.getCatalog().getAuthPolicy().getRole(roleName_);
    if (isDropRole_ && existingRole == null) {
      throw new AnalysisException(String.format("Role '%s' does not exist.", roleName_));
    } else if (!isDropRole_ && existingRole != null) {
      throw new AnalysisException(String.format("Role '%s' already exists.", roleName_));
    }
  }
}